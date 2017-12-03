import time
import pickle
import numpy as np

import theano
import theano.tensor as T
import lasagne

from supmodel import Model

from lasagne.layers import batch_norm
from layers.sampling import GaussianSampleLayer
from layers.shape import RepeatLayer

from distributions import log_bernoulli, log_normal, log_normal2

# ----------------------------------------------------------------------------

class SupervisedHDGM(Model):
  """Hybrid Discriminative/Generative Model (fully supervised version)"""
  def __init__(self, n_dim, n_out, n_chan=1, n_batch=128, n_superbatch=12800, model='bernoulli',
                opt_alg='adam', unsup_weight=1.0, sup_weight=1.0, opt_params={'lr' : 1e-3, 'b1': 0.9, 'b2': 0.99}):
    # save model that wil be created
    self.model = model
    self.n_sample = 1 # adjustable parameter, though 1 works best in practice
    self.unsup_weight=unsup_weight
    self.sup_weight=sup_weight

    self.n_batch = n_batch
    self.n_lat = 200
    self.n_dim = n_dim
    self.n_chan = n_chan
    self.n_batch = n_batch

    Model.__init__(self, n_dim, n_chan, n_out, n_superbatch, opt_alg, opt_params)

    # sample generation
    Z = T.matrix(dtype=theano.config.floatX) # noise matrix
    l_px_mu, l_px_logsigma, l_pa_mu, l_pa_logsigma, \
        l_qz_mu, l_qz_logsigma, l_qa_mu, l_qa_logsigma, \
        l_qa, l_qz, l_d  = self.network
    sample = lasagne.layers.get_output(l_px_mu,  {l_qz : Z}, deterministic=True)
    self.sample = theano.function([Z], sample, on_unused_input='warn')
  
  def create_model(self, X, Y, n_dim, n_out, n_chan=1):
    # params
    n_lat = 200 # latent stochastic variables
    n_aux = 10  # auxiliary variables
    n_hid = 499 # size of hidden layer in encoder/decoder
    n_sam = self.n_sample # number of monte-carlo samples
    n_out = n_dim * n_dim * n_chan # total dimensionality of ouput
    hid_nl = lasagne.nonlinearities.rectify
    relu_shift = lambda av: T.nnet.relu(av+10)-10 # for numerical stability

    # create the encoder network

    # create q(a|x)
    l_qa_in = lasagne.layers.InputLayer(shape=(None, n_chan, n_dim, n_dim), 
                                     input_var=X)
    l_qa_hid1 = (lasagne.layers.DenseLayer(
        l_qa_in, num_units=n_hid,
        W=lasagne.init.Orthogonal(),
        b=lasagne.init.Constant(0.0),
        nonlinearity=hid_nl))
    l_qa_mu = lasagne.layers.DenseLayer(
        l_qa_hid1, num_units=n_aux,
        W=lasagne.init.Orthogonal(),
        b=lasagne.init.Constant(0.0),
        nonlinearity=None)
    l_qa_logsigma = lasagne.layers.DenseLayer(
        l_qa_hid1, num_units=n_aux,
        W=lasagne.init.Orthogonal(),
        b=lasagne.init.Constant(0.0),
        nonlinearity=relu_shift)
    l_qa_mu = lasagne.layers.ReshapeLayer(
        RepeatLayer(l_qa_mu, n_ax=1, n_rep=n_sam),
        shape=(-1, n_aux))
    l_qa_logsigma = lasagne.layers.ReshapeLayer(
        RepeatLayer(l_qa_logsigma, n_ax=1, n_rep=n_sam),
        shape=(-1, n_aux))
    l_qa = GaussianSampleLayer(l_qa_mu, l_qa_logsigma)

    # create q(z|a,x)
    l_qz_hid1a = (lasagne.layers.DenseLayer(
        l_qa, num_units=n_hid,
        W=lasagne.init.Orthogonal(),
        b=lasagne.init.Constant(0.0),
        nonlinearity=hid_nl))
    l_qz_hid1b = (lasagne.layers.DenseLayer(
        l_qa_in, num_units=n_hid,
        W=lasagne.init.Orthogonal(),
        b=lasagne.init.Constant(0.0),
        nonlinearity=hid_nl))
    l_qz_hid1b = lasagne.layers.ReshapeLayer(
        RepeatLayer(l_qz_hid1b, n_ax=1, n_rep=n_sam),
        shape=(-1, n_hid))
    l_qz_hid2 = lasagne.layers.ElemwiseSumLayer([l_qz_hid1a, l_qz_hid1b])
    l_qz_hid2 = lasagne.layers.NonlinearityLayer(l_qz_hid2, hid_nl)
    l_qz_mu = lasagne.layers.DenseLayer(
        l_qz_hid2, num_units=n_lat,
        W=lasagne.init.Orthogonal(),
        b=lasagne.init.Constant(0.0),
        nonlinearity=None)
    l_qz_logsigma = lasagne.layers.DenseLayer(
        l_qz_hid2, num_units=n_lat,
        W=lasagne.init.Orthogonal(),
        b=lasagne.init.Constant(0.0),
        nonlinearity=relu_shift)
    l_qz = GaussianSampleLayer(l_qz_mu, l_qz_logsigma, name='l_qz')

    # create the decoder network

    # create p(x|z)
    l_px_hid1 = (lasagne.layers.DenseLayer(
        l_qz, num_units=n_hid,
        W=lasagne.init.Orthogonal(),
        b=lasagne.init.Constant(0.0),
        nonlinearity=hid_nl))

    if self.model == 'bernoulli':
      l_px_mu = lasagne.layers.DenseLayer(l_px_hid1, num_units=n_out,
          nonlinearity = lasagne.nonlinearities.sigmoid,
          W=lasagne.init.Orthogonal(),
          b=lasagne.init.Constant(0.0))
    elif self.model == 'gaussian':
      l_px_mu = lasagne.layers.DenseLayer(
          l_px_hid1, num_units=n_out,
          W=lasagne.init.Orthogonal(),
          b=lasagne.init.Constant(0.0),
          nonlinearity=None)
    l_px_logsigma = lasagne.layers.DenseLayer(
      l_px_hid1, num_units=n_out,
      W=lasagne.init.Orthogonal(),
      b=lasagne.init.Constant(0.0),
      nonlinearity=relu_shift)

    # create p(a|z)
    l_pa_hid1 = (lasagne.layers.DenseLayer(
      l_qz, num_units=n_hid,
      nonlinearity=hid_nl,
      W=lasagne.init.Orthogonal(),
      b=lasagne.init.Constant(0.0)))
    l_pa_mu = lasagne.layers.DenseLayer(
        l_pa_hid1, num_units=n_aux,
        W=lasagne.init.Orthogonal(),
        b=lasagne.init.Constant(0.0),
        nonlinearity=None)
    l_pa_logsigma = lasagne.layers.DenseLayer(
        l_pa_hid1, num_units=n_aux,
        W=lasagne.init.Orthogonal(),
        b=lasagne.init.Constant(0.0),
        nonlinearity=relu_shift)

    # discriminative model
    l_in_drop = lasagne.layers.DropoutLayer(l_qa_in, p=0.2)
    # l_in_drop = l_in

    l_conv1 = lasagne.layers.Conv2DLayer(
        l_in_drop, num_filters=128, filter_size=(5, 5),
        nonlinearity=lasagne.nonlinearities.rectify,
        pad='same', W=lasagne.init.Orthogonal())
    l_conv1 = lasagne.layers.MaxPool2DLayer(
        l_conv1, pool_size=(3, 3), stride=(2,2))
    # l_conv1 = lasagne.layers.DropoutLayer(l_conv1, p=0.5)

    l_conv2 = lasagne.layers.Conv2DLayer(
        l_conv1, num_filters=128, filter_size=(5, 5),
        nonlinearity=lasagne.nonlinearities.rectify,
        pad='same', W=lasagne.init.Orthogonal())
    l_conv2 = lasagne.layers.MaxPool2DLayer(
        l_conv2, pool_size=(3, 3), stride=(2,2))
    # l_conv2 = lasagne.layers.DropoutLayer(l_conv2, p=0.5)

    l_conv3 = lasagne.layers.Conv2DLayer(
        l_conv2, num_filters=256, filter_size=(5, 5),
        nonlinearity=lasagne.nonlinearities.rectify,
        pad='same', W=lasagne.init.Orthogonal())
    l_conv3 = lasagne.layers.MaxPool2DLayer(
        l_conv3, pool_size=(3, 3), stride=(2,2))
    # l_conv3 = lasagne.layers.DropoutLayer(l_conv3, p=0.5)
    l_conv3 = lasagne.layers.FlattenLayer(l_conv3)

    l_merge = lasagne.layers.ConcatLayer([l_conv3, l_qz_mu])

    l_hid = lasagne.layers.DenseLayer(
        l_merge, num_units=500,
        W=lasagne.init.Orthogonal(),
        b=lasagne.init.Constant(0.0),
        nonlinearity=lasagne.nonlinearities.rectify)
    l_hid_drop = lasagne.layers.DropoutLayer(l_hid, p=0.5)
    # l_hid_drop = l_hid

    l_d = lasagne.layers.DenseLayer(
            l_hid_drop, num_units=n_out,
            W=lasagne.init.Orthogonal(),
            b=lasagne.init.Constant(0.0),
            nonlinearity=lasagne.nonlinearities.softmax)

    return l_px_mu, l_px_logsigma, l_pa_mu, l_pa_logsigma, \
           l_qz_mu, l_qz_logsigma, l_qa_mu, l_qa_logsigma, \
           l_qa, l_qz, l_d

  def create_objectives(self, deterministic=False):
    # load network input
    X = self.inputs[0]
    Y = self.inputs[1]
    x = X.flatten(2)

    # duplicate entries to take into account multiple mc samples
    n_sam = self.n_sample
    n_out = x.shape[1]
    x = x.dimshuffle(0,'x',1).repeat(n_sam, axis=1).reshape((-1, n_out))

    # load network
    l_px_mu, l_px_logsigma, l_pa_mu, l_pa_logsigma, \
      l_qz_mu, l_qz_logsigma, l_qa_mu, l_qa_logsigma, \
      l_qa, l_qz, l_d = self.network
    
    # load network output
    pa_mu, pa_logsigma, qz_mu, qz_logsigma, qa_mu, qa_logsigma, a, z, px_mu, px_logsigma, P \
      = lasagne.layers.get_output(
          [ l_pa_mu, l_pa_logsigma, l_qz_mu, l_qz_logsigma, 
            l_qa_mu, l_qa_logsigma, l_qa, l_qz, l_px_mu, l_px_logsigma, l_d ],
          deterministic=deterministic)

    # entropy term
    log_qa_given_x  = log_normal2(a, qa_mu, qa_logsigma).sum(axis=1)
    log_qz_given_ax = log_normal2(z, qz_mu, qz_logsigma).sum(axis=1)
    log_qza_given_x = log_qz_given_ax + log_qa_given_x

    # log-probability term
    z_prior_sigma = T.cast(T.ones_like(qz_logsigma), dtype=theano.config.floatX)
    z_prior_mu = T.cast(T.zeros_like(qz_mu), dtype=theano.config.floatX)
    log_pz = log_normal(z, z_prior_mu,  z_prior_sigma).sum(axis=1)
    log_pa_given_z = log_normal2(a, pa_mu, pa_logsigma).sum(axis=1)

    if self.model == 'bernoulli':
      log_px_given_z = log_bernoulli(x, px_mu).sum(axis=1)
    elif self.model == 'gaussian':
      log_px_given_z = log_normal2(x, px_mu, px_logsigma).sum(axis=1)

    log_paxz = log_pa_given_z + log_px_given_z + log_pz

    # discriminative component
    P = lasagne.layers.get_output(l_d)
    P_test = lasagne.layers.get_output(l_d, deterministic=True)
    disc_loss = lasagne.objectives.categorical_crossentropy(P, Y)

    # measure accuracy
    top = theano.tensor.argmax(P, axis=-1)
    top_test = theano.tensor.argmax(P_test, axis=-1)
    acc = theano.tensor.eq(top, Y).mean()
    acc_test = theano.tensor.eq(top_test, Y).mean()

    # compute the evidence lower bound
    elbo = T.mean(-self.sup_weight*disc_loss + self.unsup_weight*(log_paxz - log_qza_given_x))
    # elbo = T.mean(-disc_loss)

    if deterministic:
      return -elbo, acc_test
    else:
      return -elbo, acc

  def create_gradients(self, loss, deterministic=False):
    grads = Model.create_gradients(self, loss, deterministic)

    # combine and clip gradients
    clip_grad = 1
    max_norm = 5
    mgrads = lasagne.updates.total_norm_constraint(grads, max_norm=max_norm)
    cgrads = [T.clip(g, -clip_grad, clip_grad) for g in mgrads]

    return cgrads

  def get_params(self):
    l_px_mu = self.network[0]
    l_pa_mu = self.network[2]
    l_d = self.network[-1]
    params  = lasagne.layers.get_all_params([l_px_mu, l_pa_mu, l_d], trainable=True)
    
    return params

  def gen_samples(self, n_sam):
    n_lat, n_dim, n_chan, n_batch = self.n_lat, self.n_dim, self.n_chan, self.n_batch
    noise = np.random.randn(n_batch, n_lat).astype(theano.config.floatX)
    # noise = np.zeros((n_sam, n_lat))
    # noise[range(n_sam), np.random.choice(n_lat, n_sam)] = 1

    assert np.sqrt(n_sam) == int(np.sqrt(n_sam))
    n_side = int(np.sqrt(n_sam))

    p_mu = self.sample(noise)
    p_mu = p_mu[:n_sam]
    p_mu = p_mu.reshape((n_side, n_side, n_chan, n_dim, n_dim))
    p_mu = p_mu[:,:,0,:,:] # keep the first channel

    # split into n_side (1,n_side,n_dim,n_dim,) images,
    # concat along columns -> 1,n_side,n_dim,n_dim*n_side
    p_mu = np.concatenate(np.split(p_mu, n_side, axis=0), axis=3)
    # split into n_side (1,1,n_dim,n_dim*n_side) images,
    # concat along rows -> 1,1,n_dim*n_side,n_dim*n_side
    p_mu = np.concatenate(np.split(p_mu, n_side, axis=1), axis=2)
    return np.squeeze(p_mu)