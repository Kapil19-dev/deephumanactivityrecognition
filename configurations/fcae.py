from os import rmdir, path
import shutil
from lasagne.nonlinearities import rectify
from data_preparation.load_data import LoadHAR
from models.fcae import CAE
from training.train import TrainModel
from utils import env_paths as paths
from sklearn.cross_validation import LeaveOneLabelOut, StratifiedShuffleSplit
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.ioff()

def main():
    n_samples, step = 200, 200
    load_data = LoadHAR(add_pitch=False, add_roll=False, add_filter=False, n_samples=n_samples,
                        step=step, normalize='segments', comp_magnitude=True, simple_labels=False, common_labels=False)

    X, y, name, users, stats = load_data.uci_hapt()
    users = ['%s%02d' % (name, user) for user in users]
    limited_labels = y < 6
    y = y[limited_labels]
    X = X[limited_labels]
    users = np.char.asarray(users)[limited_labels]

    cv = StratifiedShuffleSplit(y, n_iter=1, test_size=0.1, random_state=0)
    for (train_index, test_index) in cv:
        x_train, x_test = X[train_index], X[test_index]
        y_train, y_test = y[train_index], y[test_index]
    n_win, n_samples, n_features = x_train.shape

    train_set = (x_train, y_train)
    test_set = (x_test, y_test)
    print('Train size: ', train_set[0].shape)
    print('Test size: ', test_set[0].shape)

    n_train = train_set[0].shape[0]
    n_test = test_set[0].shape[0]
    n_test_batches = 1
    batch_size = n_test
    n_train_batches = n_train//batch_size

    model = CAE(n_in=(int(n_samples), int(n_features)),
                filter_sizes=[0],
                pool_sizes=[0],
                n_hidden=[0],
                n_out=0,
                trans_func=rectify,
                stats=0)

    # Build model
    f_train, f_test, f_validate, train_args, test_args, validate_args = model.build_model(train_set,
                                                                                          test_set,
                                                                                          None)

    def f_custom(model, path):
        out = model.get_output(test_set[0]).eval()
        plt.clf()
        f, axarr = plt.subplots(nrows=2, ncols=1)
        axarr[0].plot(test_set[0], color='red')
        axarr[1].plot(out, color='blue')

        f.set_size_inches(12, 8)
        f.savefig(path, dpi=100)
        plt.close(f)

    train = TrainModel(model=model,
                       anneal_lr=0.75,
                       anneal_lr_freq=50,
                       output_freq=1,
                       pickle_f_custom_freq=100,
                       f_custom_eval=f_custom)
    train.pickle = False

    test_args['inputs']['batchsize'] = batch_size
    train_args['inputs']['batchsize'] = batch_size
    train_args['inputs']['learningrate'] = 0.003
    train_args['inputs']['beta1'] = 0.9
    train_args['inputs']['beta2'] = 1e-6
    validate_args['inputs']['batchsize'] = batch_size

    train.train_model(f_train, train_args,
                      f_test, test_args,
                      f_validate, validate_args,
                      n_train_batches=n_train_batches,
                      n_test_batches=n_test_batches,
                      n_epochs=500)

if __name__ == "__main__":
    main()