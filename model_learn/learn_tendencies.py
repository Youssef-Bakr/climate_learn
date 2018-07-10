import argparse
from datetime import datetime
import logging
import math

# from matplotlib import pyplot as plt
import numpy as np
import pandas as pd
from sklearn import metrics
import tensorflow as tf
from tensorflow.python.data import Dataset
import xarray as xr

tf.logging.set_verbosity(tf.logging.ERROR)
pd.options.display.max_rows = 10
pd.options.display.float_format = '{:.2f}'.format

#-----------------------------------------------------------------------------------------------------------------------
# set up a basic, global _logger which will write to the console as standard error
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s',
                    datefmt='%Y-%m-%d  %H:%M:%S')
_logger = logging.getLogger(__name__)

#-----------------------------------------------------------------------------------------------------------------------
def get_input(features, 
              targets, 
              batch_size=1, 
              shuffle=True, 
              num_epochs=None):
    """
    Extracts a batch of elements from a dataset.
  
    To import our weather data into our DNNRegressor, we need to define 
    an input function, which instructs TensorFlow how to preprocess the data,
    as well as how to batch, shuffle, and repeat it during model training.
    
    First, we'll convert our xarray feature data into a dict of NumPy arrays.
    We can then use the TensorFlow Dataset API to construct a dataset object 
    from our data, and then break our data into batches of `batch_size`, to be
    repeated for the specified number of epochs (`num_epochs`).
    
    NOTE: When the default value of `num_epochs=None` is passed to `repeat()`,
    the input data will be repeated indefinitely.
    
    Next, if `shuffle` is set to True, we'll shuffle the data so that it's
    passed to the model randomly during training. The `buffer_size` argument
    specifies the size of the dataset from which shuffle will randomly sample.
    
    Finally, our input function constructs an iterator for the dataset 
    and returns the next batch of data.

    Args:
      features: xarray Dataset of features
      targets: xarray Dataset of targets
      batch_size: Size of batches to be passed to the model
      shuffle: True or False. Whether to shuffle the data.
      num_epochs: Number of epochs for which data should be repeated. 
                  None == repeat indefinitely
    Returns:
      Tuple of (features, labels) for next data batch
    """
  
    # Convert xarray data into a dict of numpy arrays.
    # Each dictionary item will be key == variable name, value == variable array
    features_dict = {var:features[var].values for var in features.variables}
    targets_dict = {var:targets[var].values for var in targets.variables}

    # Construct a dataset, and configure batching/repeating.
    ds = Dataset.from_tensor_slices((features_dict, targets_dict)) # warning: 2GB limit
    ds = ds.batch(batch_size).repeat(num_epochs)
    
    # Shuffle the data, if specified.
    if shuffle:
        ds = ds.shuffle(buffer_size=10000)
    
    # Return the next batch of data.
    features, labels = ds.make_one_shot_iterator().get_next()
    return features, labels

#-----------------------------------------------------------------------------------------------------------------------
if __name__ == '__main__':
    """
    This module is used to perform climate indices processing on gridded datasets in NetCDF.
    """

    try:

        # log some timing info, used later for elapsed time
        start_datetime = datetime.now()
        _logger.info("Start time:    %s", start_datetime)

        # parse the command line arguments
        parser = argparse.ArgumentParser()
        parser.add_argument("--netcdf_flows",
                            help="NetCDF file containing flow variables",
                            required=True)
        parser.add_argument("--netcdf_tendencies",
                            help="NetCDF file containing time tendency forcing variables",
                            required=True)
        parser.add_argument("--layers",
                            help="Number of nodes per layer",
                            type=int,
                            nargs = '*')
        args = parser.parse_args()

        
        # Load the flow and time tendency forcing datasets into Xarray Dataset objects.
        data_h0 = xr.open_dataset(args.netcdf_flows, decode_times=False)
        data_h1 = xr.open_dataset(args.netcdf_tendencies, decode_times=False)
        
        """
        ## Define the features and configure feature columns
        
        In TensorFlow, we indicate a feature's data type using a construct
        called a feature column. Feature columns store only a description 
        of the feature data; they do not contain the feature data itself.
        As features we'll use the following flow variables:
        
        * U (west-east (zonal) wind, m/s)
        * V (south-north (meridional) wind, m/s)
        * T (temperature, K)
        * PS (surface pressure, Pa)
        
        We'll take the flow variables dataset and trim out all but the
        above variables, and use this as the data source for features.
        
        The variables correspond to Numpy arrays, and we'll use the shapes of
        the variable arrays as the shapes of the corresponding feature columns.
        """
        
        # Define the input features as PS, T, U, and V.
        
        # remove all non-feature variables and unrelated coordinate variables 
        # from the dataset, in order to trim the memory footprint.
        feature_vars = ['PS', 'T', 'U', 'V']
        feature_coord_vars = ['time', 'lev', 'lat', 'lon']
        for var in data_h0.variables:
            if (var not in feature_vars) and (var not in feature_coord_vars):
                data_h0 = data_h0.drop(var)  
        features = data_h0.to_dataframe()
        
        # Configure numeric feature columns for the input features.
        feature_columns = []
        for var in feature_vars:
            feature_columns.append(tf.feature_column.numeric_column(var, shape=features[var].shape))
        
        """
        ## Define the targets (labels)
        
        Time tendency forcings are the targets (labels) that our model
        should learn to predict.
        
        * PTTEND (time tendency of the temperature)
        * PUTEND (time tendency of the zonal wind)
        * PVTEND (time tendency of the meridional wind)
        
        We'll take the time tendency forcings dataset and trim out all
        other variables so we can use this as the data source for targets.
        """
        
        # Define the targets (labels) as PTTEND, PUTEND, and PVTEND.
        
        # Remove all non-target variables and unrelated coordinate variables
        # from the DataSet, in order to trim the memory footprint.
        target_vars = ['PTTEND', 'PUTEND', 'PVTEND']
        target_coord_vars = ['time', 'lev', 'lat', 'lon']
        for var in data_h1.variables:
            if (var not in target_vars) and (var not in target_coord_vars):
                data_h1 = data_h1.drop(var)
        targets = data_h1.to_dataframe()
        
#         # Confirm the compatability of our features and targets datasets,
#         # in terms of dimensions and coordinates.
#         if features.dims != targets.dims:
#             print("WARNING: Unequal dimensions")
#         else:
#             for coord in features.coords:
#                 if not (features.coords[coord] == targets.coords[coord]).all():
#                     print("WARNING: Unequal {} coordinates".format(coord))
        
        """
        ## Split the data into training, validation, and testing datasets
        
        We'll initially split the dataset into training, validation, 
        and testing datasets with 50% for training and 25% each for 
        validation and testing. We'll use the longitude dimension 
        to split since it has 180 points and divides evenly by four.
        We get every other longitude starting at the first longitude to get
        50% of the dataset for training, then every fourth longitude
        starting at the second longitude to get 25% of the dataset for 
        validation, and every fourth longitude starting at the fourth
        longitude to get 25% of the dataset for testing.
        """
        
        lon_range_training = list(range(0, features.dims['lon'], 2))
        lon_range_validation = list(range(1, features.dims['lon'], 4))
        lon_range_testing = list(range(3, features.dims['lon'], 4))
        
        features_training = features.isel(lon=lon_range_training)
        features_validation = features.isel(lon=lon_range_validation)
        features_testing = features.isel(lon=lon_range_testing)
        
        targets_training = targets.isel(lon=lon_range_training)
        targets_validation = targets.isel(lon=lon_range_validation)
        targets_testing = targets.isel(lon=lon_range_testing)
        
        """
        ## Create the neural network
        
        Next, we'll instantiate and configure a neural network using
        TensorFlow's [DNNRegressor](https://www.tensorflow.org/api_docs/python/tf/estimator/DNNRegressor)
        class. We'll train this model using the GradientDescentOptimizer,
        which implements Mini-Batch Stochastic Gradient Descent (SGD).
        The learning_rate argument controls the size of the gradient step.
        
        NOTE: To be safe, we also apply gradient clipping to our optimizer via
        `clip_gradients_by_norm`. Gradient clipping ensures the magnitude of
        the gradients do not become too large during training, which can cause
        gradient descent to fail.
        
        We use `hidden_units`to define the structure of the NN.
        The `hidden_units` argument provides a list of ints, where each int
        corresponds to a hidden layer and indicates the number of nodes in it.
        For example, consider the following assignment:
        
        `hidden_units=[3, 10]`
        
        The preceding assignment specifies a neural net with two hidden layers:
        
        The first hidden layer contains 3 nodes.
        The second hidden layer contains 10 nodes.
        If we wanted to add more layers, we'd add more ints to the list.
        For example, `hidden_units=[10, 20, 30, 40]` would create four layers
        with ten, twenty, thirty, and forty units, respectively.
        
        By default, all hidden layers will use ReLu activation and will be fully connected.
        """
        
        # Use gradient descent as the optimizer for training the model.
        gd_optimizer = tf.train.GradientDescentOptimizer(learning_rate=0.001)
        gd_optimizer = tf.contrib.estimator.clip_gradients_by_norm(gd_optimizer, 5.0)
        
        # Use hidden layers with the number of nodes specified as command arguments.
        hidden_units = args.layers
        
        # Instantiate the neural network.
        dnn_regressor = tf.estimator.DNNRegressor(feature_columns=feature_columns,
                                                  hidden_units=hidden_units,
                                                  optimizer=gd_optimizer)
        
        # Create input functions. Wrap get_input() in a lambda so we 
        # can pass in features and targets as arguments.
        input_training = lambda: get_input(features_training, 
                                           targets_training, 
                                           batch_size=10)
        predict_input_training = lambda: get_input(features_training, 
                                                   targets_training, 
                                                   num_epochs=1, 
                                                   shuffle=False)
        predict_input_validation = lambda: get_input(features_validation, 
                                                     targets_validation, 
                                                     num_epochs=1, 
                                                     shuffle=False)
        
        """## Train and evaluate the model
        
        We can now call `train()` on our `dnn_regressor` to train the model. We'll loop over a number of periods and on each loop we'll train the model, use it to make predictions, and compute the RMSE of the loss for both training and validation datasets.
        """
        
        print("Training model...")
        print("RMSE (on training data):")
        training_rmse = []
        validation_rmse = []
        
        steps = 500
        periods = 20
        steps_per_period = steps / periods
        
        # Train the model inside a loop so that we can periodically assess loss metrics.
        for period in range (0, periods):
        
            # Train the model, starting from the prior state.
            dnn_regressor.train(input_fn=input_training,
                                steps=steps_per_period)
        
            # Take a break and compute predictions, converting to numpy arrays.
            training_predictions = dnn_regressor.predict(input_fn=predict_input_training)
            training_predictions = np.array([item['predictions'][0] for item in training_predictions])
            
            validation_predictions = dnn_regressor.predict(input_fn=predict_input_validation)
            validation_predictions = np.array([item['predictions'][0] for item in validation_predictions])
            
            # Compute training and validation loss.
            training_root_mean_squared_error = math.sqrt(
                metrics.mean_squared_error(training_predictions, targets_training))
            validation_root_mean_squared_error = math.sqrt(
                metrics.mean_squared_error(validation_predictions, targets_validation))
            
            # Print the current loss.
            print("  period %02d : %0.2f" % (period, training_root_mean_squared_error))
            
            # Add the loss metrics from this period to our list.
            training_rmse.append(training_root_mean_squared_error)
            validation_rmse.append(validation_root_mean_squared_error)
        
        print("Model training finished.")
        
#         # Output a graph of loss metrics over periods.
#         plt.ylabel("RMSE")
#         plt.xlabel("Periods")
#         plt.title("Root Mean Squared Error vs. Periods")
#         plt.tight_layout()
#         plt.plot(training_rmse, label="training")
#         plt.plot(validation_rmse, label="validation")
#         plt.legend()
        
        print("Final RMSE (on training data):   %0.2f" % training_root_mean_squared_error)
        print("Final RMSE (on validation data): %0.2f" % validation_root_mean_squared_error)

        # report on the elapsed time
        end_datetime = datetime.now()
        _logger.info("End time:      %s", end_datetime)
        elapsed = end_datetime - start_datetime
        _logger.info("Elapsed time:  %s", elapsed)

    except Exception as ex:
        _logger.exception('Failed to complete', exc_info=True)
        raise