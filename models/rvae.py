import theano
import theano.tensor as T
from lasagne import init
from models.base import Model
from lasagne_extensions.layers import (SampleLayer, MultinomialLogDensityLayer,
                                       GaussianLogDensityLayer, StandardNormalLogDensityLayer, BernoulliLogDensityLayer,
                                       InputLayer, DenseLayer, DimshuffleLayer, ElemwiseSumLayer, ReshapeLayer,
                                       NonlinearityLayer, BatchNormLayer, get_all_params, get_output)
from lasagne_extensions.layers import (RecurrentLayer, LSTMLayer, ConcatLayer, RepeatLayer, Gate, ConstrainLayer)
from lasagne_extensions.objectives import categorical_crossentropy, categorical_accuracy, aggregate, squared_error
from lasagne_extensions.nonlinearities import rectify, softplus, sigmoid, softmax
from lasagne_extensions.updates import total_norm_constraint
from lasagne_extensions.updates import rmsprop, adam, adagrad
from parmesan.distributions import log_normal
from theano.tensor.shared_randomstreams import RandomStreams
import numpy as np


class RVAE(Model):
    """
    Implementation of recurrent variational auto-encoder.
    """

    def __init__(self, n_c, n_z, qz_hid, px_hid, enc_rnn=256, dec_rnn=256, n_l=28,
                 nonlinearity=rectify, px_nonlinearity=None, x_dist='bernoulli', batchnorm=False, seed=1234):
        """
        Weights are initialized using the Bengio and Glorot (2010) initialization scheme.
        :param n_c: Number of inputs.
        :param n_z: Number of latent.
        :param qz_hid: List of number of deterministic hidden q(z|a,x,y).
        :param px_hid: List of number of deterministic hidden p(a|z,y) & p(x|z,y).
        :param nonlinearity: The transfer function used in the deterministic layers.
        :param x_dist: The x distribution, 'bernoulli', 'multinomial', or 'gaussian'.
        :param batchnorm: Boolean value for batch normalization.
        :param seed: The random seed.
        """
        super(RVAE, self).__init__(n_c, qz_hid + px_hid, n_z, nonlinearity)
        self.x_dist = x_dist
        self.n_x = n_c
        self.seq_length = n_l
        self.n_z = n_z
        self.batchnorm = batchnorm
        self._srng = RandomStreams(seed)

        # Decide Glorot initializaiton of weights.
        init_w = 1e-3
        hid_w = ""
        if nonlinearity == rectify or nonlinearity == softplus:
            hid_w = "relu"

        # Define symbolic variables for theano functions.
        self.sym_x = T.tensor3('x')  # inputs
        self.sym_z = T.matrix('z')
        self.sym_samples = T.iscalar('samples')  # MC samples
        self.sym_warmup = T.fscalar('warmup')

        # Assist methods for collecting the layers
        def dense_layer(layer_in, n, dist_w=init.GlorotNormal, dist_b=init.Normal):
            dense = DenseLayer(layer_in, num_units=n, W=dist_w(hid_w), b=dist_b(init_w), nonlinearity=None)
            if batchnorm:
                dense = BatchNormLayer(dense)
            return NonlinearityLayer(dense, self.transf)

        def stochastic_layer(layer_in, n, samples, nonlin=None):
            mu = DenseLayer(layer_in, n, W=init.Normal(init_w, mean=.0), b=init.Normal(init_w), nonlinearity=nonlin)
            logvar = DenseLayer(layer_in, n, W=init.Normal(init_w, mean=.0), b=init.Normal(init_w), nonlinearity=nonlin)
            # logvar = ConstrainLayer(logvar, scale=1, max=T.log(-0.999 * self.sym_warmup + 1.0999))
            return SampleLayer(mu, logvar, eq_samples=samples, iw_samples=1), mu, logvar

        def lstm_layer(input, nunits, return_final, backwards=False, name='LSTM'):
            ingate=Gate(W_in=init.Uniform(0.01), W_hid=init.Uniform(0.01), b=init.Constant(0.0))
            forgetgate=Gate(W_in=init.Uniform(0.01), W_hid=init.Uniform(0.01), b=init.Constant(5.0))
            cell=Gate(W_cell=None, nonlinearity=T.tanh,W_in=init.Uniform(0.01), W_hid=init.Uniform(0.01),)
            outgate=Gate(W_in=init.Uniform(0.01), W_hid=init.Uniform(0.01), b=init.Constant(0.0))

            lstm = LSTMLayer(input, num_units=nunits, backwards=backwards,
                             peepholes=False,
                             ingate=ingate,
                             forgetgate=forgetgate,
                             cell=cell,
                             outgate=outgate,
                             name=name,
                             only_return_final=return_final)
            return lstm

        # RNN encoder implementation
        l_x_in = InputLayer((None, n_l, n_c))
        l_enc_forward = lstm_layer(l_x_in, enc_rnn, return_final=True, backwards=False, name='enc_forward')
        l_enc_backward = lstm_layer(l_x_in, enc_rnn, return_final=True, backwards=True, name='enc_backward')
        l_enc_concat = ConcatLayer([l_enc_forward, l_enc_backward], axis=-1)
        l_enc = dense_layer(l_enc_concat, enc_rnn)

        # # Overwrite encoder
        # l_enc = dense_layer(l_x_in, enc_rnn)

        # Recognition q(z|x)
        l_qz = l_enc
        for hid in qz_hid:
            l_qz = dense_layer(l_qz, hid)

        # Reparameterisation and sample
        l_qz_mu = DenseLayer(l_qz, n_z, W=init.Normal(init_w, mean=1.0), b=init.Normal(init_w), nonlinearity=None)
        l_qz_logvar = DenseLayer(l_qz, n_z, init.Normal(init_w), init.Normal(init_w), nonlinearity=None)
        l_qz = SampleLayer(l_qz_mu, l_qz_logvar, eq_samples=self.sym_samples, iw_samples=1)

        # Generative p(x|z)
        l_qz_repeat = RepeatLayer(l_qz, n=n_l)

        # Skip connection to encoder until warmup threshold is reached
        if T.ge(self.sym_warmup, 0.4):
            l_skip_enc_repeat = RepeatLayer(l_enc, n=n_l)
            l_qz_repeat = ConcatLayer([l_qz_repeat, l_skip_enc_repeat], axis=-1)

        l_dec_forward = lstm_layer(l_qz_repeat, dec_rnn, return_final=False, backwards=False, name='dec_forward')
        l_dec_backward = lstm_layer(l_qz_repeat, dec_rnn, return_final=False, backwards=True, name='dec_backward')
        l_dec_concat = ConcatLayer([l_dec_forward, l_dec_backward], axis=-1)
        l_dec = ReshapeLayer(l_dec_concat, (-1, 2*dec_rnn))
        l_dec = dense_layer(l_dec, dec_rnn)

        # # Overwrite decoder
        # l_dec = dense_layer(l_qz, n_l)

        # Add additional dense layers
        l_px = l_dec
        for hid in px_hid:
            l_px = dense_layer(l_px, hid)

        # Reshape the last dimension and perhaps model with a distribution
        if x_dist == 'bernoulli':
            l_px = DenseLayer(l_px, n_c, init.GlorotNormal(), init.Normal(init_w), sigmoid)
        elif x_dist == 'multinomial':
            l_px = DenseLayer(l_px, n_c, init.GlorotNormal(), init.Normal(init_w), softmax)
        elif x_dist == 'gaussian':
            l_px, l_px_mu, l_px_logvar = stochastic_layer(l_px, n_c, self.sym_samples, nonlin=px_nonlinearity)
        elif x_dist == 'linear':
            l_px = DenseLayer(l_px, n_c, nonlinearity=None)

        # Reshape all the model layers to have the same size
        self.l_x_in = l_x_in

        self.l_qz = ReshapeLayer(l_qz, (-1, self.sym_samples, 1, n_z))
        self.l_qz_mu = DimshuffleLayer(l_qz_mu, (0, 'x', 'x', 1))
        self.l_qz_logvar = DimshuffleLayer(l_qz_logvar, (0, 'x', 'x', 1))

        self.l_px = DimshuffleLayer(ReshapeLayer(l_px, (-1, n_l, self.sym_samples, 1, n_c)), (0, 2, 3, 1, 4))
        self.l_px_mu = DimshuffleLayer(ReshapeLayer(l_px_mu, (-1, n_l, self.sym_samples, 1, n_c)), (0, 2, 3, 1, 4)) \
            if x_dist == "gaussian" else None
        self.l_px_logvar = DimshuffleLayer(ReshapeLayer(l_px_logvar, (-1, n_l, self.sym_samples, 1, n_c)), (0, 2, 3, 1, 4)) \
            if x_dist == "gaussian" else None

        # Predefined functions
        inputs = {self.l_x_in: self.sym_x}
        outputs = get_output(l_qz, inputs, deterministic=True)
        self.f_qz = theano.function([self.sym_x, self.sym_samples], outputs, on_unused_input='warn')

        inputs = {l_qz: self.sym_z, self.l_x_in: self.sym_x}
        outputs = get_output(self.l_px, inputs, deterministic=True).mean(axis=(1, 2))
        self.f_px = theano.function([self.sym_x, self.sym_z, self.sym_samples], outputs, on_unused_input='warn')

        if x_dist == "gaussian":
            outputs = get_output(self.l_px_mu, inputs, deterministic=True).mean(axis=(1, 2))
            self.f_mu = theano.function([self.sym_x, self.sym_z, self.sym_samples], outputs, on_unused_input='ignore')

            outputs = get_output(self.l_px_logvar, inputs, deterministic=True).mean(axis=(1, 2))
            self.f_var = theano.function([self.sym_x, self.sym_z, self.sym_samples], outputs, on_unused_input='ignore')

        # Define model parameters
        self.model_params = get_all_params([self.l_px])
        self.trainable_model_params = get_all_params([self.l_px], trainable=True)

    def build_model(self, train_set, test_set, validation_set=None):
        """
        :param train_set_unlabeled: Unlabeled train set containing variables x, t.
        :param train_set_labeled: Unlabeled train set containing variables x, t.
        :param test_set: Test set containing variables x, t.
        :param validation_set: Validation set containing variables x, t.
        :return: train, test, validation function and dicts of arguments.
        """
        super(RVAE, self).build_model(train_set, test_set, validation_set)

        n = self.sh_train_x.shape[0].astype(theano.config.floatX)  # no. of data points

        # Define the layers for the density estimation used in the lower bound.
        l_log_qz = GaussianLogDensityLayer(self.l_qz, self.l_qz_mu, self.l_qz_logvar)
        l_log_pz = StandardNormalLogDensityLayer(self.l_qz)

        l_x_in = ReshapeLayer(self.l_x_in, (-1, self.seq_length * self.n_x))
        if self.x_dist == 'bernoulli':
            l_px = ReshapeLayer(self.l_px, (-1, self.sym_samples, 1, self.seq_length * self.n_x))
            l_log_px = BernoulliLogDensityLayer(l_px, l_x_in)
        elif self.x_dist == 'multinomial':
            l_px = ReshapeLayer(self.l_px, (-1, self.sym_samples, 1, self.seq_length * self.n_x))
            l_log_px = MultinomialLogDensityLayer(l_px, l_x_in)
        elif self.x_dist == 'gaussian':
            l_px_mu = ReshapeLayer(self.l_px_mu, (-1, self.sym_samples, 1, self.seq_length * self.n_x))
            l_px_logvar = ReshapeLayer(self.l_px_logvar, (-1, self.sym_samples, 1, self.seq_length * self.n_x))
            l_log_px = GaussianLogDensityLayer(l_x_in, l_px_mu, l_px_logvar)
        elif self.x_dist == 'linear':
            l_log_px = self.l_px

        def lower_bound(log_pz, log_qz, log_px, warmup=self.sym_warmup):
            return log_px + (log_pz - log_qz)*(1.1 - warmup)

        # Lower bound
        out_layers = [l_log_pz, l_log_qz, l_log_px]
        inputs = {self.l_x_in: self.sym_x}
        out = get_output(out_layers, inputs, batch_norm_update_averages=False, batch_norm_use_averages=False)
        log_pz, log_qz, log_px = out

        # If the decoder output is linear we need the reconstruction error
        if self.x_dist == 'linear':
            log_px = -aggregate(squared_error(log_px.mean(axis=(1, 2)), self.sym_x), mode='mean')

        lb = lower_bound(log_pz, log_qz, log_px)
        lb = lb.mean(axis=(1, 2))  # Mean over the sampling dimensions

        # if self.batchnorm:
            # TODO: implement the BN layer correctly.
            # inputs = {self.l_x_in: self.sym_x}
            # get_output(out_layers, inputs, weighting=None, batch_norm_update_averages=True, batch_norm_use_averages=False)

        # Regularizing with weight priors p(theta|N(0,1)), collecting and clipping gradients
        weight_priors = 0.0
        for p in self.trainable_model_params:
            if 'W' not in str(p):
                continue
            weight_priors += log_normal(p, 0, 1).sum()

        # Collect the lower bound and scale it with the weight priors.
        elbo = lb.mean()
        cost = (elbo * n + weight_priors) / -n

        # Add reconstruction cost
        # xhat = get_output(self.l_px, inputs).mean(axis=(1, 2))
        # cost += aggregate(squared_error(xhat, self.sym_x), mode='mean')

        grads_collect = T.grad(cost, self.trainable_model_params)
        sym_beta1 = T.scalar('beta1')
        sym_beta2 = T.scalar('beta2')
        clip_grad, max_norm = 1, 5
        mgrads = total_norm_constraint(grads_collect, max_norm=max_norm)
        mgrads = [T.clip(g, -clip_grad, clip_grad) for g in mgrads]
        updates = adam(mgrads, self.trainable_model_params, self.sym_lr, sym_beta1, sym_beta2)
        # updates = rmsprop(mgrads, self.trainable_model_params, self.sym_lr + (0*sym_beta1*sym_beta2))

        # Training function
        x_batch = self.sh_train_x[self.batch_slice]
        if self.x_dist == 'bernoulli':  # Sample bernoulli input.
            x_batch = self._srng.binomial(size=x_batch.shape, n=1, p=x_batch, dtype=theano.config.floatX)

        givens = {self.sym_x: x_batch}
        inputs = [self.sym_index, self.sym_batchsize, self.sym_lr, sym_beta1, sym_beta2, self.sym_samples, self.sym_warmup]
        outputs = [log_px.mean(), log_pz.mean(), log_qz.mean(), elbo, self.sym_warmup]
        f_train = theano.function(inputs=inputs, outputs=outputs, givens=givens, updates=updates, on_unused_input='warn')

        # Default training args. Note that these can be changed during or prior to training.
        self.train_args['inputs']['batchsize'] = 100
        self.train_args['inputs']['learningrate'] = 1e-4
        self.train_args['inputs']['beta1'] = 0.9
        self.train_args['inputs']['beta2'] = 0.999
        self.train_args['inputs']['samples'] = 1
        self.train_args['inputs']['warmup'] = 0.1
        self.train_args['outputs']['px'] = '%0.4f'
        self.train_args['outputs']['pz'] = '%0.4f'
        self.train_args['outputs']['qz'] = '%0.4f'
        self.train_args['outputs']['lb train'] = '%0.4f'
        self.train_args['outputs']['warmup'] = '%0.3f'

        # Validation and test function
        givens = {self.sym_x: self.sh_test_x}
        f_test = theano.function(inputs=[self.sym_samples, self.sym_warmup], outputs=[elbo], givens=givens, on_unused_input='warn')

        # Test args.  Note that these can be changed during or prior to training.
        self.test_args['inputs']['samples'] = 1
        self.test_args['inputs']['warmup'] = 0.1
        self.test_args['outputs']['lb test'] = '%0.4f'

        f_validate = None
        if validation_set is not None:
            givens = {self.sym_x: self.sh_valid_x}
            f_validate = theano.function(inputs=[self.sym_samples], outputs=[elbo], givens=givens)
            # Default validation args. Note that these can be changed during or prior to training.
            self.validate_args['inputs']['samples'] = 1
            self.validate_args['outputs']['elbo validation'] = '%0.6f'

        return f_train, f_test, f_validate, self.train_args, self.test_args, self.validate_args

    def get_output(self, x, samples=1):
        return self.f_px(x, samples)

    def model_info(self):
        s = ""
        s += 'batch norm: %s.\n' % (str(self.batchnorm))
        s += 'x distribution: %s.' % (str(self.x_dist))
        return s
