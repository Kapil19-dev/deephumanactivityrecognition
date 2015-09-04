from __future__ import print_function
"""
Based on implementation from Zheng, 2014, Time series classification
using multi-channels deep convolutional neural networks
"""

import theano.sandbox.cuda
theano.sandbox.cuda.use('gpu0')

import datetime
import os
import theano
import theano.tensor as T
import lasagne
import load_data as ld
import numpy as np
import lasagne.updates
import itertools
import time
import pandas as pd
import matplotlib
matplotlib.use("Agg")

NAME = "1D_CNN"
# Number of input units (window samples)
N_UNITS = 128
# Number of units in the hidden (recurrent) layer
N_HIDDEN = 100
# Number of training sequences in each batch
BATCH_SIZE = 100
# Number of features
N_FEATURES = 3
# Optimization learning rate
LEARNING_RATE = .001
# All gradients above this will be clipped
GRAD_CLIP = 100
# How often should we check the output?
EPOCH_SIZE = 100
# Number of epochs to train the net
NUM_EPOCHS = 500
# Momentum
MOMENTUM = 0.9
DROPOUT = 0.5
INPUT_DROPOUT = 0.2

# Path to HAR data
if 'nt' in os.name:
    ROOT_FOLDER = 'D:/PhD/Data/activity/'
else:
    ROOT_FOLDER = '/home/sdka/data/activity'


def load_data():
    data = ld.LoadHAR(ROOT_FOLDER).uci_har_v1()

    return dict(
        output_dim=int(data['y_test'].shape[-1]),
        X_train=theano.shared(data['x_train'].astype(theano.config.floatX)),
        y_train=theano.shared(
            data['y_train'].astype(theano.config.floatX)
        ),
        X_valid=theano.shared(data['x_test'].astype(theano.config.floatX)),
        y_valid=theano.shared(
            data['y_test'].astype(theano.config.floatX)
        ),
        X_test=theano.shared(data['x_test'].astype(theano.config.floatX)),
        y_test=theano.shared(
            data['y_test'].astype(theano.config.floatX)
        ),
        num_examples_train=data['x_train'].shape[0],
        num_examples_valid=data['x_test'].shape[0],
        num_examples_test=data['x_test'].shape[0],
        seq_len=int(data['x_train'].shape[1]),
        n_fea=int(data['x_train'].shape[2])
        )


def build_model(output_dim, batch_size=BATCH_SIZE, seq_len=N_UNITS, n_features=N_FEATURES):
    x = np.random.random((batch_size, seq_len, n_features)).astype(theano.config.floatX)
    sym_x = T.tensor3('x')

    print("Building network ...")
    # First, we build the network, starting with an input layer
    # Recurrent layers expect input of shape
    # (batch size, max sequence length, number of features)
    l_in = lasagne.layers.InputLayer(
        shape=(batch_size, seq_len, n_features)
    )

    # Reshape to single layer
    # l_reshp = lasagne.layers.ReshapeLayer(l_in, (batch_size, seq_len*n_features, 1))

    # Input dropout for regularization
    l_do = lasagne.layers.dropout(l_in, p=INPUT_DROPOUT)

    # Reshape layer for convolution so we have (batch_size, num_input_channels, input_length)
    l_dim = lasagne.layers.DimshuffleLayer(l_do, (0, 2, 1))
    print("Conv input shape", lasagne.layers.get_output(l_dim, sym_x).eval({sym_x: x}).shape)

    # Convolution layers. Convolution and pooling
    l_conv = lasagne.layers.Conv1DLayer(l_dim,  num_filters=10, filter_size=3, stride=1, pad=1, nonlinearity=None)
    l_pool = lasagne.layers.MaxPool1DLayer(l_conv, pool_size=2)

    l_conv2 = lasagne.layers.Conv1DLayer(l_dim,  num_filters=10, filter_size=3, stride=1, pad=1)
    l_pool2 = lasagne.layers.MaxPool1DLayer(l_conv2, pool_size=2)

    l_concat = lasagne.layers.ConcatLayer([l_pool, l_pool2], axis=1)

    l_conv3 = lasagne.layers.Conv1DLayer(l_concat,  num_filters=20, filter_size=3, stride=1, pad=1)
    print("Conv output shape", lasagne.layers.get_output(l_concat, sym_x).eval({sym_x: x}).shape)

    # Reshape to batch_size x all samples to classify the whole batch
    # l_reshp = lasagne.layers.ReshapeLayer(l_conv, (batch_size, -1))
    # print("Dense input shape", lasagne.layers.get_output(l_reshp, sym_x).eval({sym_x: x}).shape)

    # Add dense layers
    l_dense = lasagne.layers.DenseLayer(l_conv3, num_units=512)
    l_dense = lasagne.layers.DenseLayer(l_dense, num_units=512)
    l_do = lasagne.layers.dropout(l_dense, p=0.5)

    # Our output layer is a simple dense connection, with n_classes output unit
    l_out = lasagne.layers.DenseLayer(
        l_do,
        num_units=output_dim,
        nonlinearity=lasagne.nonlinearities.softmax
    )

    return l_out


def cross_ent_cost(predicted_values, target_values):
    return -T.sum(target_values*T.log(predicted_values + 1e-8))/T.sum(target_values)


def create_iter_functions(dataset, output_layer,
                          batch_size=BATCH_SIZE,
                          learning_rate=LEARNING_RATE,
                          momentum=MOMENTUM):
    """
    Create functions for training, validation and testing to iterate one epoch.
    """
    batch_index = T.iscalar('batch_index')
    X_batch = T.tensor3('input')
    y_batch = T.matrix('target_output')
    batch_slice = slice(batch_index * batch_size,
                        (batch_index + 1) * batch_size)

    prediction = T.argmax(
        lasagne.layers.get_output(output_layer, X_batch, deterministic=True),
        axis=-1
    )
    accuracy = T.mean(T.eq(prediction, T.argmax(y_batch, axis=-1)), dtype=theano.config.floatX)

    loss_train = cross_ent_cost(
        lasagne.layers.get_output(output_layer, X_batch, deterministic=False),
        y_batch
    )

    loss_eval = cross_ent_cost(
        lasagne.layers.get_output(output_layer, X_batch, deterministic=True),
        y_batch
    )

    all_params = lasagne.layers.get_all_params(output_layer)
    updates = lasagne.updates.adam(
        loss_train,
        all_params,
        learning_rate=learning_rate
    )

    iter_train = theano.function(
        [batch_index], loss_train,
        updates=updates,
        givens={
            X_batch: dataset['X_train'][batch_slice],
            y_batch: dataset['y_train'][batch_slice],
        },
    )

    iter_valid = theano.function(
        [batch_index], [loss_eval, accuracy],
        givens={
            X_batch: dataset['X_valid'][batch_slice],
            y_batch: dataset['y_valid'][batch_slice],
        },
    )

    iter_test = theano.function(
        [batch_index], [loss_eval, accuracy],
        givens={
            X_batch: dataset['X_test'][batch_slice],
            y_batch: dataset['y_test'][batch_slice],
        },
    )

    return dict(
        train=iter_train,
        valid=iter_valid,
        test=iter_test,
    )


def train(iter_funcs, dataset, batch_size=BATCH_SIZE):
    """Train the model with `dataset` with mini-batch training. Each
       mini-batch has `batch_size` recordings.
    """
    num_batches_train = dataset['num_examples_train'] // batch_size
    num_batches_valid = dataset['num_examples_valid'] // batch_size

    for epoch in itertools.count(1):
        batch_train_losses = []
        for b in range(num_batches_train):
            batch_train_loss = iter_funcs['train'](b)
            batch_train_losses.append(batch_train_loss)

        avg_train_loss = np.mean(batch_train_losses)

        batch_valid_losses = []
        batch_valid_accuracies = []
        for b in range(num_batches_valid):
            batch_valid_loss, batch_valid_accuracy = iter_funcs['valid'](b)
            batch_valid_losses.append(batch_valid_loss)
            batch_valid_accuracies.append(batch_valid_accuracy)

        avg_valid_loss = np.mean(batch_valid_losses)
        avg_valid_accuracy = np.mean(batch_valid_accuracies)

        yield {
            'number': epoch,
            'train_loss': avg_train_loss,
            'valid_loss': avg_valid_loss,
            'valid_accuracy': avg_valid_accuracy,
        }


def main(num_epochs=NUM_EPOCHS):
    print("Loading data...")
    dataset = load_data()

    print("Building model and compiling functions...")
    output_layer = build_model(
        output_dim=dataset['output_dim'],
        batch_size=BATCH_SIZE,
        seq_len=dataset['seq_len'],
        n_features=dataset['n_fea']
        )

    iter_funcs = create_iter_functions(
        dataset,
        output_layer,
        )

    results = []
    print("Starting training...")
    now = time.time()
    try:
        for epoch in train(iter_funcs, dataset):
            print("{}: epoch {} of {} took {:.3f}s | training loss: {:.6f} "
                  "| validation loss: {:.6f} | validation accuracy: {:.2f} %%".
                  format(NAME, epoch['number'], num_epochs, time.time() - now, epoch['train_loss'],
                         epoch['valid_loss'], epoch['valid_accuracy'] * 100))

            results.append([epoch['train_loss'], epoch['valid_loss'], epoch['valid_accuracy']])

            now = time.time()
            if epoch['train_loss'] is np.nan:
                break
            if epoch['number'] >= num_epochs:
                break

        # Save figure
        ax = pd.DataFrame(np.asarray(results), columns=['Training loss', 'Validation loss', 'Validation accuracy'])\
            .plot()
        fig = ax.get_figure()
        fig.savefig("%s-%s" % (NAME, datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")))

    except KeyboardInterrupt:
        pass

    return output_layer

if __name__ == '__main__':
    output = main()
