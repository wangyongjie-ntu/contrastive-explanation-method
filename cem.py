""" This module implements the Contrastive Explanation Method in Pytorch.

Paper:  https://arxiv.org/abs/1802.07623
"""

import numpy as np
import torch

class ContrastiveExplanationMethod:
    
    def __init__(
        self,
        classifier,
        autoencoder = None,
        kappa: float = 0.,
        const: float = 10.,
        beta: float = .1,
        gamma: float = 0.,
        feature_range: tuple = (-1e10, 1e10),
        iterations: int = 1000,
        n_searches: int = 9,
        learning_rate: float = 0.1,
        batch: bool = False,
    ):
        """
        Initialise the CEM model.
        
        classifier
            classification model to be explained.
        mode
            for pertinant negatives 'PN' or for pertinant positives 'PP'.
        autoencoder
            optional, autoencoder to be used for regularisation of the
            modifications to the explained samples.
        kappa
            confidence parameter used in the loss functions (eq. 2) and (eq. 4) in
            the original paper.
        const
            initial regularisation coefficient for the attack loss term.
        beta
            regularisation coefficent for the L1 term of the optimisation objective.
        gamma
            regularisation coefficient for the autoencoder term of the optimisation
            objective.
        feature_range
            range over which the features of the perturbed instances should be distributed.
        """
        classifier.train()
        autoencoder.train()
        self.classifier = classifier
        self.autoencoder = autoencoder
        self.kappa = kappa
        self.c_init = const
        self.c = const
        self.beta = beta
        self.gamma = gamma
        self.feature_range = feature_range
        self.iterations = iterations
        self.n_searches = n_searches
        self.learning_rate = learning_rate

        # if input is batch (as opposed to single sample), reduce dimensions along second axis, otherwise reduce along first axis
        self.reduce_dim = int(batch)

    def fista(self, orig_sample, mode="PN"):
        """Fast Iterative Shrinkage Thresholding Algorithm implementation in pytorch
        
        Paper: https://doi.org/10.1137/080716542
        
        (Eq. 5) and (eq. 6) in https://arxiv.org/abs/1802.07623
        """

        # initialise search values
        self.mode = mode
        # self.delta = torch.zeros(orig_sample.shape, requires_grad=True)
        # self.y = torch.zeros(orig_sample.shape, requires_grad=True)

        orig_sample = orig_sample.view(28*28)

        perturb_init = torch.zeros(orig_sample.shape, requires_grad=True)

        # to keep track of whether in the current search the perturbation loss reached 0
        self.loss_reached_zero = False

        self.best_delta = None
        self.best_loss = float("Inf")
        self.prev_deltas = []

        # projection space for binary datasets (X/x_0) for PN and (x_0) for PP used in (eq. 5, 6)
        if mode == "PN":
            self.pert_space = (torch.ones(orig_sample.shape) - orig_sample)
            self.pert_space /= torch.norm(self.pert_space)
        elif mode == "PP":
            self.pert_space = orig_sample.clone()
            self.pert_space /= torch.norm(self.pert_space)

        # See appendix A
        for s in range(self.n_searches):

            # initialise values for a new search
            self.c = self.c_init
            delta = torch.zeros(orig_sample.shape, requires_grad=True)
            y = torch.zeros(orig_sample.shape, requires_grad=True)
            orig_sample = orig_sample.clone().detach().requires_grad_(True)

            for i in range(1, self.iterations + 1):

                # Reset the computational graph, otherwise we get a multiple backward passes error
                delta = delta.clone().detach().requires_grad_(True)
                y = y.clone().detach().requires_grad_(True)

                # if self.y.grad is not None:
                #     self.y.grad.data.zero_()
                #     self.delta.grad.data.zero_()
                #     self.perturb_init.grad.data.zero_()

                print("search iteration: {}".format(i))

                # calculate loss as per (eq. 1, 3)
                obj = (self.optimisation_obj(orig_sample, y)).sum()
                self.loss = obj
                print("current loss: {}".format(obj.item()))

                if obj < self.best_loss:
                    self.best_delta = delta
                    self.best_loss = obj

                # calculate gradients
                y.retain_grad()
                delta.retain_grad()
                orig_sample.retain_grad()

                obj.backward()
                
                self.prev_deltas.append(delta.clone().detach())

                # project onto subspace that contains our possible features. (eq. 5, 6)
                delta = self.pert_space.dot(self.shrink(y - self.learning_rate * y.grad)) * self.pert_space
                y = self.pert_space.dot((delta + i/(i + 3)*(delta - self.prev_deltas[-1]))) * self.pert_space

            if self.loss_reached_zero:
                self.c = (self.c + self.c_init) / 2
            else:
                self.c *= 10

    def shrink(self, z):
        """Element-wise shrinkage thresholding function.
        
        (Eq. 7) in https://arxiv.org/abs/1802.07623
        """
        zeros = torch.zeros(z.shape)
        z_min = z - self.beta
        z_plus = z + self.beta
        
        z_shrunk = z.clone()
        z_shrunk = torch.where(torch.abs(z) <= self.beta, zeros, z_shrunk)
        z_shrunk = torch.where(z > self.beta, z_min, z_shrunk)
        z_shrunk = torch.where(z < -self.beta, z_plus, z_shrunk)
        return z_shrunk
                        
    def optimisation_obj(self, orig_sample, y):
        """
        Optimisation objective for PN (eq. 1) and for PP (eq. 3).
        """

        obj = (
            self.c * self.loss_fn(orig_sample, y) +
            self.beta * torch.sum(torch.abs(y)) +
            torch.norm(y) ** 2
        )

        if callable(self.autoencoder):
            if self.mode == "PN":
                obj += self.gamma * torch.norm(orig_sample + y - self.autoencoder((orig_sample + y).view(-1, 1, 28, 28)).view(28*28)) ** 2 # TEMP FIX
            elif self.mode == "PP":
                obj += self.gamma * torch.norm(y - self.autoencoder(y.view(-1, 1, 28, 28)).view(28*28)) ** 2  # TEMP FIX
        return obj

    def loss_fn(self, orig_sample, y):
        """
        Loss term f(x,d) for PN (eq. 2) and for PP (eq. 4).
        
        orig_sample
            the unperturbed original sample, batch size first.
        """
        
        orig_output = self.classifier(orig_sample.view(-1, 1, 28, 28))
        target_mask = torch.zeros(orig_output.shape)
        target_mask[torch.arange(orig_output.shape[0]), torch.argmax(orig_output)] = 1
        nontarget_mask = torch.ones(orig_output.shape) - target_mask
        
        min_kappa = torch.tensor(-self.kappa, requires_grad=True)

        if self.mode == "PN":
            pert_output = self.classifier((orig_sample + y).view(-1, 1, 28, 28))

            #perturbation_loss = torch.where()

            perturbation_loss = torch.max(
                torch.max(target_mask * pert_output) - 
                torch.max(nontarget_mask * pert_output),
                min_kappa
            )
        elif self.mode == "PP":
            pert_output = self.classifier(y.view(-1, 1, 28, 28))
            perturbation_loss = torch.max(
                torch.max(nontarget_mask * pert_output) -
                torch.max(target_mask * pert_output),
                min_kappa
            )

        if perturbation_loss.item() == -self.kappa:
            self.loss_reached_zero = True

        self.loss = perturbation_loss

        return perturbation_loss
    
