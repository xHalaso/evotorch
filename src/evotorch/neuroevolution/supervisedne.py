# Copyright 2022 NNAISENSE SA
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from ast import Import
from collections.abc import Mapping
from typing import Any, Callable, List, Optional, Union
from warnings import warn

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader, Dataset

from ..core import BoundsPairLike, SolutionBatch
from ..tools.misc import Device
from .neproblem import NEProblem


class SupervisedNE(NEProblem):
    """
    Representation of a neuro-evolution problem where the goal is to minimize
    a loss function in a supervised learning setting.

    A supervised learning problem can be defined via subclassing this class
    and overriding the methods
    `_loss(y_hat, y)` (which is to define how the loss is computed)
    and `_make_dataloader()` (which is to define how a new DataLoader is
    created).

    Alternatively, this class can be directly instantiated as follows:

    ```python
    def my_loss_function(output_of_network, desired_output):
        loss = ...  # compute the loss here
        return loss


    problem = SupervisedNE(
        my_dataset, MyTorchModuleClass, my_loss_function, minibatch_size=..., ...
    )
    ```
    """

    def __init__(
        self,
        dataset: Dataset,
        network: Union[str, nn.Module, Callable[[], nn.Module]],
        loss_func: Optional[Callable] = None,
        *,
        network_args: Optional[dict] = None,
        initial_bounds: Optional[BoundsPairLike] = (-0.00001, 0.00001),
        minibatch_size: Optional[int] = None,
        num_minibatches: Optional[int] = None,
        num_actors: Optional[Union[int, str]] = None,
        common_minibatch: bool = True,
        num_gpus_per_actor: Optional[Union[int, float, str]] = None,
        actor_config: Optional[dict] = None,
        num_subbatches: Optional[int] = None,
        subbatch_size: Optional[int] = None,
        device: Optional[Device] = None,
    ):
        """
        `__init__(...)`: Initialize the SupervisedNE.

        Args:
            dataset: The Dataset from which the minibatches will be pulled
            network: A network structure string, or a Callable (which can be
                a class inheriting from `torch.nn.Module`, or a function
                which returns a `torch.nn.Module` instance), or an instance
                of `torch.nn.Module`.
                The object provided here determines the structure of the
                neural network whose parameters will be evolved.
                A network structure string is a string which can be processed
                by `evotorch.neuroevolution.net.str_to_net(...)`.
                Please see the documentation of the function
                `evotorch.neuroevolution.net.str_to_net(...)` to see how such
                a neural network structure string looks like.
            loss_func: Optionally a function (or a Callable object) which
                receives `y_hat` (the output generated by the neural network)
                and `y` (the desired output), and returns the loss as a
                scalar.
                This argument can also be left as None, in which case it will
                be expected that the method `_loss(self, y_hat, y)` is
                overridden by the inheriting class.
            network_args: Optionally a dict-like object, storing keyword
                arguments to be passed to the network while instantiating it.
            initial_bounds: Specifies an interval from which the values of the
                initial neural network parameters will be drawn.
            minibatch_size: Optionally an integer, describing the size of a
                minibatch when pulling data from the dataset.
                Can also be left as None, in which case it will be expected
                that the inheriting class overrides the method
                `_make_dataloader()` and defines how a new DataLoader is to be
                made.
            num_minibatches: An integer, specifying over how many minibatches
                will a single neural network be evaluated.
                If not specified, it will be assumed that the desired number
                of minibatches per network evaluation is 1.
            num_actors: Number of actors to create for parallelized
                evaluation of the solutions.
                Certain string values are also accepted.
                When given as "max" or as "num_cpus", the number of actors
                will be equal to the number of all available CPUs in the ray
                cluster.
                When given as "num_gpus", the number of actors will be
                equal to the number of all available GPUs in the ray
                cluster, and each actor will be assigned a GPU.
                When given as "num_devices", the number of actors will be
                equal to the minimum among the number of CPUs and the number
                of GPUs available in the cluster (or will be equal to the
                number of CPUs if there is no GPU), and each actor will be
                assigned a GPU (if available).
                If `num_actors` is given as "num_gpus" or "num_devices",
                the argument `num_gpus_per_actor` must not be used,
                and the `actor_config` dictionary must not contain the
                key "num_gpus".
                If `num_actors` is given as something other than "num_gpus"
                or "num_devices", and if you wish to assign GPUs to each
                actor, then please see the argument `num_gpus_per_actor`.
            common_minibatch: Whether the same minibatches will be
                used when evaluating the solutions or not.
            actor_config: A dictionary, representing the keyword arguments
                to be passed to the options(...) used when creating the
                ray actor objects. To be used for explicitly allocating
                resources per each actor.
                For example, for declaring that each actor is to use a GPU,
                one can pass `actor_config=dict(num_gpus=1)`.
                Can also be given as None (which is the default),
                if no such options are to be passed.
            num_gpus_per_actor: Number of GPUs to be allocated by each
                remote actor.
                The default behavior is to NOT allocate any GPU at all
                (which is the default behavior of the ray library as well).
                When given as a number `n`, each actor will be given
                `n` GPUs (where `n` can be an integer, or can be a `float`
                for fractional allocation).
                When given as a string "max", then the available GPUs
                across the entire ray cluster (or within the local computer
                in the simplest cases) will be equally distributed among
                the actors.
                When given as a string "all", then each actor will have
                access to all the GPUs (this will be achieved by suppressing
                the environment variable `CUDA_VISIBLE_DEVICES` for each
                actor).
                When the problem is not distributed (i.e. when there are
                no actors), this argument is expected to be left as None.
            num_subbatches: If `num_subbatches` is None (assuming that
                `subbatch_size` is also None), then, when evaluating a
                population, the population will be split into n pieces, `n`
                being the number of actors, and each actor will evaluate
                its assigned piece. If `num_subbatches` is an integer `m`,
                then the population will be split into `m` pieces,
                and actors will continually accept the next unevaluated
                piece as they finish their current tasks.
                The arguments `num_subbatches` and `subbatch_size` cannot
                be given values other than None at the same time.
                While using a distributed algorithm, this argument determines
                how many sub-batches will be generated, and therefore,
                how many gradients will be computed by the remote actors.
            subbatch_size: If `subbatch_size` is None (assuming that
                `num_subbatches` is also None), then, when evaluating a
                population, the population will be split into `n` pieces, `n`
                being the number of actors, and each actor will evaluate its
                assigned piece. If `subbatch_size` is an integer `m`,
                then the population will be split into pieces of size `m`,
                and actors will continually accept the next unevaluated
                piece as they finish their current tasks.
                When there can be significant difference across the solutions
                in terms of computational requirements, specifying a
                `subbatch_size` can be beneficial, because, while one
                actor is busy with a subbatch containing computationally
                challenging solutions, other actors can accept more
                tasks and save time.
                The arguments `num_subbatches` and `subbatch_size` cannot
                be given values other than None at the same time.
                While using a distributed algorithm, this argument determines
                the size of a sub-batch (or sub-population) sampled by a
                remote actor for computing a gradient.
                In distributed mode, it is expected that the population size
                is divisible by `subbatch_size`.
            device: Default device in which a new population will be generated
                and the neural networks will operate.
                If not specified, "cpu" will be used.
        """
        super().__init__(
            objective_sense="min",
            network=network,
            network_args=network_args,
            initial_bounds=initial_bounds,
            num_actors=num_actors,
            num_gpus_per_actor=num_gpus_per_actor,
            actor_config=actor_config,
            num_subbatches=num_subbatches,
            subbatch_size=subbatch_size,
            device=device,
        )

        self.dataset = dataset
        self.dataloader: DataLoader = None

        self._loss_func = loss_func
        self._minibatch_size = None if minibatch_size is None else int(minibatch_size)
        self._num_minibatches = 1 if num_minibatches is None else int(num_minibatches)
        self._common_minibatch = common_minibatch
        self._current_minibatches: Optional[list] = None

    def _make_dataloader(self) -> DataLoader:
        """
        Make a new DataLoader.

        This method, in its default state, does not contain an implementation.
        In the case where the `__init__` of `SupervisedNE` is not provided
        with a minibatch size, it will be expected that this method is
        overridden by the inheriting class and that the operation of creating
        a new DataLoader is defined here.

        Returns:
            The new DataLoader.
        """
        raise NotImplementedError

    def make_dataloader(self) -> DataLoader:
        """
        Make a new DataLoader.

        If the `__init__` of `SupervisedNE` was provided with a minibatch size
        via the argument `minibatch_size`, then a new DataLoader will be made
        with that minibatch size.
        Otherwise, it will be expected that the method `_make_dataloader(...)`
        was overridden to contain details regarding how the DataLoader should be
        created, and that method will be executed.

        Returns:
            The created DataLoader.
        """
        if self._minibatch_size is None:
            return self._make_dataloader()
        else:
            return DataLoader(self.dataset, shuffle=True, batch_size=self._minibatch_size)

    def _evaluate_using_minibatch(self, network: nn.Module, batch: Any) -> Union[float, torch.Tensor]:
        """
        Pass a minibatch through a network, and compute the loss.

        Args:
            network: The network using which the loss will be computed.
            batch: The minibatch that will be used as data.
        Returns:
            The loss.
        """
        with torch.no_grad():
            x, y = batch
            yhat = network(x)
            return self.loss(yhat, y)

    def _loss(self, y_hat: Any, y: Any) -> Union[float, torch.Tensor]:
        """
        The loss function.

        This method, in its default state, does not contain an implementation.
        In the case where `__init__` of `SupervisedNE` class was not given
        a loss function via the argument `loss_func`, it will be expected
        that this method is overridden by the inheriting class and that the
        operation of computing the loss is defined here.

        Args:
            y_hat: The output estimated by the network
            y: The desired output
        Returns:
            A scalar, representing the loss
        """
        raise NotImplementedError

    def loss(self, y_hat: Any, y: Any) -> Union[float, torch.Tensor]:
        """
        Run the loss function and return the loss.

        If the `__init__` of `SupervisedNE` class was given a loss
        function via the argument `loss_func`, then that loss function
        will be used. Otherwise, it will be expected that the method
        `_loss(...)` is overriden with a loss definition, and that method
        will be used to compute the loss.
        The computed loss will be returned.

        Args:
            y_hat: The output estimated by the network
            y: The desired output
        Returns:
            A scalar, representing the loss
        """
        if self._loss_func is None:
            return self._loss(y_hat, y)
        else:
            return self._loss_func(y_hat, y)

    def _prepare(self) -> None:
        self.dataloader = self.make_dataloader()

    def get_minibatch(self) -> Any:
        """
        Get the next minibatch from the DataLoader.
        """
        if self.dataloader is None:
            self._prepare()

        try:
            batch = next(self.dataloader_iterator)
            if batch is None:
                self.dataloader_iterator = iter(self.dataloader)
                batch = self.get_minibatch()
            else:
                batch = batch
        except Exception:
            self.dataloader_iterator = iter(self.dataloader)
            batch = self.get_minibatch()

        # Move batch to device of network
        return [var.to(self.network_device) for var in batch]

    def _evaluate_network(self, network: nn.Module) -> torch.Tensor:
        loss = 0.0
        for batch_idx in range(self._num_minibatches):
            if not self._common_minibatch:
                self._current_minibatch = self.get_minibatch()
            else:
                self._current_minibatch = self._current_minibatches[batch_idx]
            loss += self._evaluate_using_minibatch(network, self._current_minibatch) / self._num_minibatches
        return loss

    def _evaluate_batch(self, batch: SolutionBatch):
        if self._common_minibatch:
            # If using a common data batch, generate them now and use them for the entire batch of solutions
            self._current_minibatches = [self.get_minibatch() for _ in range(self._num_minibatches)]
        return super()._evaluate_batch(batch)
