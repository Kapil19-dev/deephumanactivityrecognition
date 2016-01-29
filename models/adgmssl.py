import theano
import theano.tensor as T
from lasagne import init
from base import Model
from lasagne_extensions.layers import (SampleLayer, GaussianMarginalLogDensityLayer, MultinomialLogDensityLayer,
                                       GaussianLogDensityLayer, StandardNormalLogDensityLayer, BernoulliLogDensityLayer,
                                       InputLayer, DenseLayer, DimshuffleLayer, ElemwiseSumLayer, ReshapeLayer,
                                       NonlinearityLayer, NormalizeLayer, ScaleAndShiftLayer, get_all_params, get_output)
from lasagne_extensions.objectives import categorical_crossentropy
from lasagne_extensions.nonlinearities import rectify, softplus, sigmoid, softmax, tanh
from lasagne_extensions.updates import total_norm_constraint
from lasagne_extensions.updates import adam, rmsprop, adagrad
from parmesan.distributions import log_normal
from theano.tensor.shared_randomstreams import RandomStreams
import numpy as np


class ADGMSSL(Model):
    """
    The :class:'ADGMSSL' class represents the implementation of the model described in
    http://approximateinference.org/accepted/MaaloeEtAl2015.pdf.
    """

    def __init__(self, n_x, n_a, n_z, n_y, a_hidden, z_hidden, xhat_hidden, y_hidden, trans_func=rectify,
                 x_dist='bernoulli', batchnorm=False, seed=1234):
        """
        Initialize an auxiliary deep generative model consisting of
        discriminative classifier q(y|a,x),
        generative model P p(xhat|z,y),
        inference model Q q(a|x) and q(z|x,y).
        All weights are initialized using the Bengio and Glorot (2010) initialization scheme.
        :param n_x: Number of inputs.
        :param n_a: Number of auxiliary.
        :param n_z: Number of latent.
        :param n_y: Number of classes.
        :param a_hidden: List of number of deterministic hidden q(a|x).
        :param z_hidden: List of number of deterministic hidden q(z|x,y).
        :param xhat_hidden: List of number of deterministic hidden p(xhat|z,y).
        :param y_hidden: List of number of deterministic hidden q(y|a,x).
        :param trans_func: The transfer function used in the deterministic layers.
        :param x_dist: The x distribution, 'bernoulli' or 'gaussian'.
        """
        super(ADGMSSL, self).__init__(n_x, a_hidden + z_hidden + xhat_hidden, n_a + n_z, trans_func)
        self.y_hidden = y_hidden
        self.x_dist = x_dist
        self.n_y = n_y
        self.n_x = n_x
        self.n_a = n_a
        self.n_z = n_z
        self.batchnorm = batchnorm

        self._srng = RandomStreams(seed)

        self.sym_beta = T.scalar('beta')  # symbolic upscaling of the discriminative term.
        self.sym_x_l = T.matrix('x')  # symbolic labeled inputs
        self.sym_t_l = T.matrix('t')  # symbolic labeled targets
        self.sym_x_u = T.matrix('x')  # symbolic unlabeled inputs
        self.sym_bs_l = T.iscalar('bs_l')  # symbolic number of labeled data_preparation points in batch
        self.sym_samples = T.iscalar('samples')  # symbolic number of Monte Carlo samples
        self.sym_y = T.matrix('y')
        self.sym_z = T.matrix('z')

        ### Input layers ###
        l_x_in = InputLayer((None, n_x))
        l_y_in = InputLayer((None, n_y))

        ### Auxiliary q(a|x) ###
        l_a_x = l_x_in
        for hid in a_hidden:
            l_a_x = DenseLayer(l_a_x, hid, init.GlorotNormal('relu'), init.Constant(0.), self.transf)
            if batchnorm:
                l_a_x = NormalizeLayer(l_a_x)
                l_a_x = ScaleAndShiftLayer(l_a_x)
        l_a_x_mu = DenseLayer(l_a_x, n_a, init.GlorotNormal(), init.Constant(0.), None)
        l_a_x_logvar = DenseLayer(l_a_x, n_a, init.GlorotNormal(), init.Constant(0.), None)
        l_a_x = SampleLayer(l_a_x_mu, l_a_x_logvar, eq_samples=self.sym_samples)
        # Reshape all layers to align them for multiple samples in the lower bound calculation.
        l_a_x_reshaped = ReshapeLayer(l_a_x, (-1, self.sym_samples, 1, n_a))
        l_a_x_mu_reshaped = DimshuffleLayer(l_a_x_mu, (0, 'x', 'x', 1))
        l_a_x_logvar_reshaped = DimshuffleLayer(l_a_x_logvar, (0, 'x', 'x', 1))

        ### Classifier q(y|a,x) ###
        # Concatenate the input x and the output of the auxiliary MLP.
        l_a_to_y = DenseLayer(l_a_x, y_hidden[0], init.GlorotNormal('relu'), init.Constant(0.), None)
        l_a_to_y = ReshapeLayer(l_a_to_y, (-1, self.sym_samples, 1, y_hidden[0]))
        l_x_to_y = DenseLayer(l_x_in, y_hidden[0], init.GlorotNormal('relu'), init.Constant(0.), None)
        l_x_to_y = DimshuffleLayer(l_x_to_y, (0, 'x', 'x', 1))
        l_y_xa = ReshapeLayer(ElemwiseSumLayer([l_a_to_y, l_x_to_y]), (-1, y_hidden[0]))
        l_y_xa = NonlinearityLayer(l_y_xa, self.transf)
        if batchnorm:
            l_y_xa = NormalizeLayer(l_y_xa)
            l_y_xa = ScaleAndShiftLayer(l_y_xa)

        # l_y_xa = DenseLayer(l_a_x, y_hidden[0], init.GlorotNormal('relu'), init.Constant(0.), self.transf)
        if len(y_hidden) > 1:
            for hid in y_hidden[1:]:
                l_y_xa = DenseLayer(l_y_xa, hid, init.GlorotNormal('relu'), init.Constant(0.), self.transf)
                if batchnorm:
                    l_y_xa = NormalizeLayer(l_y_xa)
                    l_y_xa = ScaleAndShiftLayer(l_y_xa)

        l_y_xa = DenseLayer(l_y_xa, n_y, init.GlorotNormal(), init.Constant(0.), softmax)
        l_y_xa_reshaped = ReshapeLayer(l_y_xa, (-1, self.sym_samples, 1, n_y))

        ### Recognition q(z|x,y) ###
        # Concatenate the input x and y.
        l_a_to_z = DenseLayer(l_a_x, z_hidden[0], init.GlorotNormal('relu'), init.Constant(0.), None)
        l_a_to_z = ReshapeLayer(l_a_to_z, (-1, self.sym_samples, 1, z_hidden[0]))
        l_x_to_z = DenseLayer(l_x_in, z_hidden[0], init.GlorotNormal('relu'), init.Constant(0.), None)
        l_x_to_z = DimshuffleLayer(l_x_to_z, (0, 'x', 'x', 1))
        l_y_to_z = DenseLayer(l_y_in, z_hidden[0], init.GlorotNormal('relu'), init.Constant(0.), None)
        l_y_to_z = DimshuffleLayer(l_y_to_z, (0, 'x', 'x', 1))
        l_z_xy = ReshapeLayer(ElemwiseSumLayer([l_a_to_z, l_x_to_z, l_y_to_z]), (-1, z_hidden[0]))
        l_z_xy = NonlinearityLayer(l_z_xy, self.transf)
        if batchnorm:
            l_z_xy = NormalizeLayer(l_z_xy)
            l_z_xy = ScaleAndShiftLayer(l_z_xy)

        if len(z_hidden) > 1:
            for hid in z_hidden[1:]:
                l_z_xy = DenseLayer(l_z_xy, hid, init.GlorotNormal('relu'), init.Constant(0.), self.transf)
                if batchnorm:
                    l_z_xy = NormalizeLayer(l_z_xy)
                    l_z_xy = ScaleAndShiftLayer(l_z_xy)
        l_z_axy_mu = DenseLayer(l_z_xy, n_z, init.GlorotNormal(), init.Constant(0.), None)
        l_z_axy_logvar = DenseLayer(l_z_xy, n_z, init.GlorotNormal(), init.Constant(0.), None)
        l_z_xy = SampleLayer(l_z_axy_mu, l_z_axy_logvar, eq_samples=1)
        # Reshape all layers to align them for multiple samples in the lower bound calculation.
        l_z_axy_mu_reshaped = DimshuffleLayer(l_z_axy_mu, (0, 'x', 'x', 1))
        l_z_axy_logvar_reshaped = DimshuffleLayer(l_z_axy_logvar, (0, 'x', 'x', 1))
        l_z_axy_reshaped = ReshapeLayer(l_z_xy, (-1, self.sym_samples, 1, n_z))

        ### Generative p(ahat|z,y) ###
        l_y_to_ahat = DenseLayer(l_y_in, xhat_hidden[0], init.GlorotNormal('relu'), init.Constant(0.), None)
        l_y_to_ahat = DimshuffleLayer(l_y_to_ahat, (0, 'x', 'x', 1))
        l_z_to_ahat = DenseLayer(l_z_xy, xhat_hidden[0], init.GlorotNormal('relu'), init.Constant(0.), None)
        l_z_to_ahat = ReshapeLayer(l_z_to_ahat, (-1, self.sym_samples, 1, xhat_hidden[0]))
        l_ahat_zy = ReshapeLayer(ElemwiseSumLayer([l_z_to_ahat, l_y_to_ahat]), [-1, xhat_hidden[0]])
        # l_ahat_zy = ReshapeLayer(ElemwiseSumLayer([l_z_to_ahat]), [-1, xhat_hidden[0]])
        l_ahat_zy = NonlinearityLayer(l_ahat_zy, self.transf)
        if batchnorm:
            l_ahat_zy = NormalizeLayer(l_ahat_zy)
            l_ahat_zy = ScaleAndShiftLayer(l_ahat_zy)
        # l_ahat_z = l_z_xy
        if len(xhat_hidden) > 1:
            for hid in xhat_hidden[1:]:
                l_ahat_zy = DenseLayer(l_ahat_zy, hid, init.GlorotNormal('relu'), init.Constant(0.), self.transf)
                if batchnorm:
                    l_ahat_zy = NormalizeLayer(l_ahat_zy)
                    l_ahat_zy = ScaleAndShiftLayer(l_ahat_zy)
        l_ahat_z_mu = DenseLayer(l_ahat_zy, n_a, init.GlorotNormal(), init.Constant(0.), None)
        l_ahat_z_logvar = DenseLayer(l_ahat_zy, n_a, init.Constant(0.), init.Constant(0.), None)
        l_ahat_z = SampleLayer(l_ahat_z_mu, l_ahat_z_logvar, eq_samples=1)

        l_ahat_z_reshaped = ReshapeLayer(l_ahat_z, (-1, self.sym_samples, 1, n_a))
        l_ahat_z_reshaped_mu = ReshapeLayer(l_ahat_z_mu, (-1, self.sym_samples, 1, n_a))
        l_ahat_z_reshaped_logvar = ReshapeLayer(l_ahat_z_logvar, (-1, self.sym_samples, 1, n_a))

        ### Generative p(xhat|z,y) ###
        # Concatenate the input x and y.
        l_y_to_xhat = DenseLayer(l_y_in, xhat_hidden[0], init.GlorotNormal('relu'), init.Constant(0.), None)
        l_y_to_xhat = DimshuffleLayer(l_y_to_xhat, (0, 'x', 'x', 1))
        l_z_to_xhat = DenseLayer(l_z_xy, xhat_hidden[0], init.GlorotNormal('relu'), init.Constant(0.), None)
        l_z_to_xhat = ReshapeLayer(l_z_to_xhat, (-1, self.sym_samples, 1, xhat_hidden[0]))
        l_xhat_zy = ReshapeLayer(ElemwiseSumLayer([l_z_to_xhat, l_y_to_xhat]), [-1, xhat_hidden[0]])
        l_xhat_zy = NonlinearityLayer(l_xhat_zy, self.transf)
        if batchnorm:
            l_xhat_zy = NormalizeLayer(l_xhat_zy)
            l_xhat_zy = ScaleAndShiftLayer(l_xhat_zy)
        if len(xhat_hidden) > 1:
            for hid in xhat_hidden[1:]:
                l_xhat_zy = DenseLayer(l_xhat_zy, hid, init.GlorotNormal('relu'), init.Constant(0.), self.transf)
                if batchnorm:
                    l_xhat_zy = NormalizeLayer(l_xhat_zy)
                    l_xhat_zy = ScaleAndShiftLayer(l_xhat_zy)
        if x_dist == 'bernoulli':
            l_xhat_zy_mu_reshaped = None
            l_xhat_zy_logvar_reshaped = None
            l_xhat_zy = DenseLayer(l_xhat_zy, n_x, init.GlorotNormal(), init.Normal(1e-3), sigmoid)
        elif x_dist == 'multinomial':
            l_xhat_zy_mu_reshaped = None
            l_xhat_zy_logvar_reshaped = None
            l_xhat_zy = DenseLayer(l_xhat_zy, n_x, init.GlorotNormal(), init.Normal(1e-3), softmax)
        elif x_dist == 'gaussian':
            # l_xhat_zy_mu = DenseLayer(l_xhat_zy, n_x, init.GlorotNormal('relu'), init.Constant(0.), softplus)
            # l_xhat_zy_logvar = DenseLayer(l_xhat_zy, n_x, init.GlorotNormal('relu'), init.Constant(0.), softplus)

            l_xhat_zy_mu = DenseLayer(l_xhat_zy, n_x, init.GlorotNormal(), init.Constant(0.), None)
            l_xhat_zy_logvar = DenseLayer(l_xhat_zy, n_x, init.GlorotNormal(), init.Constant(0.), None)


            l_xhat_zy = SampleLayer(l_xhat_zy_mu, l_xhat_zy_logvar, eq_samples=1)
            l_xhat_zy_mu_reshaped = ReshapeLayer(l_xhat_zy_mu, (-1, self.sym_samples, 1, n_x))
            l_xhat_zy_logvar_reshaped = ReshapeLayer(l_xhat_zy_logvar, (-1, self.sym_samples, 1, n_x))
        l_xhat_zy_reshaped = ReshapeLayer(l_xhat_zy, (-1, self.sym_samples, 1, n_x))

        ### Various class variables ###
        self.l_x_in = l_x_in
        self.l_y_in = l_y_in
        self.l_a_mu = l_a_x_mu_reshaped
        self.l_a_logvar = l_a_x_logvar_reshaped
        self.l_a = l_a_x_reshaped
        self.l_a_in = l_a_x
        self.l_z_mu = l_z_axy_mu_reshaped
        self.l_z_logvar = l_z_axy_logvar_reshaped
        self.l_z = l_z_axy_reshaped
        self.l_y = l_y_xa_reshaped
        self.l_xhat_mu = l_xhat_zy_mu_reshaped
        self.l_xhat_logvar = l_xhat_zy_logvar_reshaped
        self.l_xhat = l_xhat_zy_reshaped

        self.l_ahat = l_ahat_z_reshaped
        self.l_ahat_mu = l_ahat_z_reshaped_mu
        self.l_ahat_logvar = l_ahat_z_reshaped_logvar



        ### Calculate networks shapes for documentation ###
        self.qa_shapes = self.get_model_shape(get_all_params(l_a_x))
        self.qy_shapes = self.get_model_shape(get_all_params(l_y_xa))[len(self.qa_shapes) - 1:]
        self.qz_shapes = self.get_model_shape(get_all_params(l_z_xy))
        self.px_shapes = self.get_model_shape(get_all_params(l_xhat_zy))[(len(self.qz_shapes) - 1):]

        ### Predefined functions for generating z, xhat and y ###
        inputs = {self.l_x_in: self.sym_x_l, self.l_y_in: self.sym_y}
        outputs = get_output(self.l_z, inputs, deterministic=True).mean(axis=(1, 2))
        inputs = [self.sym_x_l, self.sym_y, self.sym_samples]
        self.f_z = theano.function(inputs, outputs)

        inputs = {l_z_xy: self.sym_z, self.l_y_in: self.sym_y}
        outputs = get_output(self.l_xhat, inputs, deterministic=True).mean(axis=(1, 2))
        inputs = [self.sym_z, self.sym_y, self.sym_samples]
        self.f_xhat = theano.function(inputs, outputs)

        inputs = [self.sym_x_l, self.sym_samples]
        outputs = get_output(self.l_y, self.sym_x_l, deterministic=True).mean(axis=(1, 2))
        self.f_y = theano.function(inputs, outputs)

        self.model_params = get_all_params([self.l_y, self.l_a, self.l_ahat, self.l_xhat], regularizable=True, trainable=True)

    def build_model(self, train_set_unlabeled, train_set_labeled, test_set, validation_set=None):
        """
        Build the auxiliary deep generative model from the initialized hyperparameters.
        Define the lower bound term and compile it into a training function.
        :param train_set_unlabeled: Unlabeled train set containing variables x, t.
        :param train_set_labeled: Unlabeled train set containing variables x, t.
        :param test_set: Test set containing variables x, t.
        :param validation_set: Validation set containing variables x, t.
        :return: train, test, validation function and dicts of arguments.
        """
        super(ADGMSSL, self).build_model(train_set_unlabeled, test_set, validation_set)

        self.sh_train_x_l = theano.shared(np.asarray(train_set_labeled[0], dtype=theano.config.floatX), borrow=True)
        self.sh_train_t_l = theano.shared(np.asarray(train_set_labeled[1], dtype=theano.config.floatX), borrow=True)

        # Define the layers for the density estimation used in the lower bound.
        # l_log_pa = GaussianMarginalLogDensityLayer(self.l_a_mu, self.l_a_logvar)
        # l_log_pz = GaussianMarginalLogDensityLayer(self.l_z_mu, self.l_z_logvar)
        # l_log_qa_x = GaussianMarginalLogDensityLayer(1, self.l_a_logvar)
        # l_log_qz_xy = GaussianMarginalLogDensityLayer(1, self.l_z_logvar)

        # l_log_pa = StandardNormalLogDensityLayer(self.l_a)
        l_log_pz = StandardNormalLogDensityLayer(self.l_z)
        l_log_qa_x = GaussianLogDensityLayer(self.l_a, self.l_a_mu, self.l_a_logvar)
        l_log_qz_xy = GaussianLogDensityLayer(self.l_z, self.l_z_mu, self.l_z_logvar)
        l_log_pa = GaussianLogDensityLayer(self.l_a, self.l_ahat_mu, self.l_ahat_logvar)

        l_log_qy_ax = MultinomialLogDensityLayer(self.l_y, self.l_y_in, eps=1e-8)
        if self.x_dist == 'bernoulli':
            l_log_px_zy = BernoulliLogDensityLayer(self.l_xhat, self.l_x_in)
        elif self.x_dist == 'multinomial':
            l_log_px_zy = MultinomialLogDensityLayer(self.l_xhat, self.l_x_in)
        elif self.x_dist == 'gaussian':
            l_log_px_zy = GaussianLogDensityLayer(self.l_x_in, self.l_xhat_mu, self.l_xhat_logvar)

        ### Compute lower bound for labeled data_preparation ###
        out_layers = [l_log_pa, l_log_pz, l_log_qa_x, l_log_qz_xy, l_log_px_zy, l_log_qy_ax]
        inputs = {self.l_x_in: self.sym_x_l, self.l_y_in: self.sym_t_l}
        log_pa_l, log_pz_l, log_qa_x_l, log_qz_axy_l, log_px_zy_l, log_qy_ax_l = get_output(out_layers, inputs)
        py_l = softmax(T.zeros((self.sym_x_l.shape[0], self.n_y)))  # non-informative prior
        log_py_l = -categorical_crossentropy(py_l, self.sym_t_l).reshape((-1, 1)).dimshuffle((0, 'x', 'x', 1))
        lb_l = log_pa_l + log_pz_l + log_py_l + log_px_zy_l - log_qz_axy_l - log_qa_x_l
        # Upscale the discriminative term with a weight.
        log_qy_ax_l *= self.sym_beta
        lb_l += log_qy_ax_l
        lb_l = lb_l.mean(axis=(1, 2))

        ### Compute lower bound for unlabeled data_preparation ###
        bs_u = self.sym_x_u.shape[0]  # size of the unlabeled data.
        t_eye = T.eye(self.n_y, k=0)  # ones in diagonal and 0's elsewhere (bs x n_y).
        # repeat unlabeled t the number of classes for integration (bs * n_y) x n_y.
        t_u = t_eye.reshape((self.n_y, 1, self.n_y)).repeat(bs_u, axis=1).reshape((-1, self.n_y))
        # repeat unlabeled x the number of classes for integration (bs * n_y) x n_x
        x_u = self.sym_x_u.reshape((1, bs_u, self.n_x)).repeat(self.n_y, axis=0).reshape((-1, self.n_x))

        # rewrite
        a_x_u = get_output(self.l_a, self.sym_x_u)
        a_x_u_rep = a_x_u.reshape((1, bs_u*self.sym_samples, self.n_a)).repeat(self.n_y, axis=0).reshape((-1, self.n_a))

        out_layers = [l_log_pa, l_log_pz, l_log_qa_x, l_log_qz_xy, l_log_px_zy, l_log_qy_ax]
        inputs = {self.l_x_in: x_u, self.l_y_in: t_u, self.l_a_in: a_x_u_rep}
        log_pa_u, log_pz_u, log_qa_x_u, log_qz_axy_u, log_px_zy_u, log_qy_ax_u = get_output(out_layers, inputs)
        py_u = softmax(T.zeros((bs_u * self.n_y, self.n_y)))  # non-informative prior.
        log_py_u = -categorical_crossentropy(py_u, t_u).reshape((-1, 1)).dimshuffle((0, 'x', 'x', 1))
        lb_u = log_pa_u + log_pz_u + log_py_u + log_px_zy_u - log_qa_x_u - log_qz_axy_u + log_qy_ax_u
        lb_u = lb_u.reshape((self.n_y, self.sym_samples, 1, bs_u)).transpose(3, 1, 2, 0).mean(axis=(1, 2))

        inputs = {self.l_x_in: self.sym_x_u, self.l_a_in: a_x_u.reshape((-1, self.n_a))}
        y_ax_u = get_output(self.l_y, inputs).mean(axis=(1, 2))
        lb_u = (y_ax_u * lb_u).sum(axis=1)

        # Loss - regularizing with weight priors p(theta|N(0,1)) and clipping gradients
        weight_priors = 0.0
        for p in self.model_params:
            if 'W' not in str(p):
                continue
            weight_priors += log_normal(p, 0, 1).sum()
        n = self.sh_train_x.shape[0].astype(theano.config.floatX)  # no. of data points in train set

        # Collect the lower bound and scale it with the weight priors.
        elbo = ((lb_l.mean() + lb_u.mean()) * n + weight_priors) / -n
        lb_u = -lb_u.mean()
        lb_l = -lb_l.mean()
        lb_labeled = lb_l
        lb_unlabeled = lb_u
        out_px_zy = log_px_zy_u.mean() + log_px_zy_l.mean()
        out_a = (log_qa_x_l.mean() + log_qa_x_u.mean())  #(log_pa_l.mean() + log_pa_u.mean()) -
        out_z = (log_qz_axy_l.mean() + log_qz_axy_u.mean())  #(log_pz_l.mean() + log_pz_u.mean()) -

        # Avoid vanishing and exploding gradients.
        clip_grad, max_norm = 1, 5
        grads = T.grad(elbo, self.model_params)
        mgrads = total_norm_constraint(grads, max_norm=max_norm)
        mgrads = [T.clip(g, -clip_grad, clip_grad) for g in mgrads]
        sym_beta1 = T.scalar('beta1')
        sym_beta2 = T.scalar('beta2')
        updates = adam(mgrads, self.model_params, self.sym_lr, sym_beta1, sym_beta2)

        ### Compile training function ###
        indices = self._srng.choice(size=[self.sym_bs_l], a=self.sh_train_x_l.shape[0], replace=False)
        x_batch_l = self.sh_train_x_l[indices]
        t_batch_l = self.sh_train_t_l[indices]
        x_batch_u = self.sh_train_x[self.batch_slice]
        if self.x_dist == 'bernoulli':  # Sample bernoulli input.
            x_batch_u = self._srng.binomial(size=x_batch_u.shape, n=1, p=x_batch_u, dtype=theano.config.floatX)
            x_batch_l = self._srng.binomial(size=x_batch_l.shape, n=1, p=x_batch_l, dtype=theano.config.floatX)

        givens = {self.sym_x_l: x_batch_l,
                  self.sym_x_u: x_batch_u,
                  self.sym_t_l: t_batch_l}
        inputs = [self.sym_index, self.sym_batchsize, self.sym_bs_l, self.sym_beta,
                  self.sym_lr, sym_beta1, sym_beta2, self.sym_samples]
        outputs = [elbo, lb_labeled, lb_unlabeled, out_px_zy, out_a, out_z]
        f_train = theano.function(inputs=inputs, outputs=outputs, givens=givens, updates=updates)
        # Default training args. Note that these can be changed during or prior to training.
        self.train_args['inputs']['batchsize_unlabeled'] = 200
        self.train_args['inputs']['batchsize_labeled'] = 100
        self.train_args['inputs']['beta'] = 1200.
        self.train_args['inputs']['learningrate'] = 3e-4
        self.train_args['inputs']['beta1'] = 0.9
        self.train_args['inputs']['beta2'] = 0.999
        self.train_args['inputs']['samples'] = 1
        self.train_args['outputs']['lb'] = '%0.4f'
        self.train_args['outputs']['lb_labeled'] = '%0.4f'
        self.train_args['outputs']['lb_unlabeled'] = '%0.4f'
        self.train_args['outputs']['px_zy'] = '%0.4f'
        self.train_args['outputs']['log p(a)-q(a)'] = '%0.4f'
        self.train_args['outputs']['log p(z)-q(z)'] = '%0.4f'

        ### Compile testing function ###
        class_err_test = self._classification_error(self.sym_x_l, self.sym_t_l)
        givens = {self.sym_x_l: self.sh_test_x,
                  self.sym_t_l: self.sh_test_t}
        f_test = theano.function(inputs=[self.sym_samples], outputs=[class_err_test], givens=givens)
        # Testing args.  Note that these can be changed during or prior to training.
        self.test_args['inputs']['samples'] = 1
        self.test_args['outputs']['err'] = '%0.2f%%'

        ### Compile validation function ###
        f_validate = None
        if validation_set is not None:
            class_err_valid = self._classification_error(self.sym_x_l, self.sym_t_l)
            givens = {self.sym_x_l: self.sh_valid_x,
                      self.sym_t_l: self.sh_valid_t}
            inputs = [self.sym_samples]
            f_validate = theano.function(inputs=[self.sym_samples], outputs=[class_err_valid], givens=givens)
        # Default validation args. Note that these can be changed during or prior to training.
        self.validate_args['inputs']['samples'] = 1
        self.validate_args['outputs']['err'] = '%0.2f%%'

        ### Collect function ###
        if self.batchnorm:
            inputs = {self.l_x_in: x_u, self.l_y_in: t_u}
            out_layers = [self.l_a, self.l_ahat, self.l_xhat, self.l_y]
            collect = get_output(out_layers, inputs, deterministic=True, collect=True)
            inputs = [self.sym_samples]
            givens = {self.sym_x_u: self.sh_train_x}
            self.f_collect = theano.function(inputs=inputs, outputs=collect, givens=givens)


        return f_train, f_test, f_validate, self.train_args, self.test_args, self.validate_args

    def _classification_error(self, x, t):
        y = get_output(self.l_y, x, deterministic=True).mean(axis=(1, 2))  # Mean over samples.
        t_class = T.argmax(t, axis=1)
        y_class = T.argmax(y, axis=1)
        accuracy = T.sum(T.eq(y_class, t_class))
        return (accuracy.astype(theano.config.floatX) / t.shape[0].astype(theano.config.floatX)) * 100.

    def after_epoch(self):
        if self.batchnorm:
            self.f_collect(self.train_args['inputs']['samples'])

    def get_output(self, x, samples=1):
        return self.f_y(x, samples)

    def model_info(self):
        s = ""
        s += 'model q(a|x): %s.\n' % str(self.qa_shapes)[1:-1]
        s += 'model q(z|x,y): %s.\n' % str(self.qz_shapes)[1:-1]
        s += 'model p(x|z,y): %s.\n' % str(self.px_shapes)[1:-1]
        s += 'model q(y|a,x): %s.' % str(self.qy_shapes)[1:-1]
        return s
