from collections import defaultdict
from typing import Union, List, Dict, Sequence

from baikal._core.data_placeholder import is_data_placeholder_list, DataPlaceholder
from baikal._core.digraph import DiGraph
from baikal._core.step import Step, InputStep
from baikal._core.typing import ArrayLike
from baikal._core.utils import find_duplicated_items, listify, safezip2, SimpleCache


class Model(Step):
    def __init__(self,
                 inputs: [DataPlaceholder, List[DataPlaceholder]],
                 outputs: [DataPlaceholder, List[DataPlaceholder]],
                 name=None, trainable=True):
        super(Model, self).__init__(name=name, trainable=trainable)

        # TODO: Check that inputs come from InputSteps
        inputs = listify(inputs)
        if not is_data_placeholder_list(inputs):
            raise ValueError('inputs must be of type DataPlaceholder.')
        if len(set(inputs)) != len(inputs):
            raise ValueError('inputs must be unique.')

        outputs = listify(outputs)
        if not is_data_placeholder_list(outputs):
            raise ValueError('outputs must be of type DataPlaceholder.')
        if len(set(outputs)) != len(outputs):
            raise ValueError('outputs must be unique.')

        self.n_outputs = len(outputs)
        self._internal_inputs = inputs
        self._internal_outputs = outputs
        self._graph = None
        self._data_placeholders = None
        self._steps = None
        self._all_steps_sorted = None
        self._steps_cache = None
        self._build()

    def _build(self):
        # Model uses the DiGraph data structure to store and operate on its DataPlaceholder and Steps.
        self._graph = build_graph_from_outputs(self._internal_outputs)

        # Collect data placeholders
        self._data_placeholders = {}
        for step in self._graph:
            for output in step.outputs:
                self._data_placeholders[output.name] = output

        # Collect steps
        self._steps = {step.name: step for step in self._graph}

        self._all_steps_sorted = self._graph.topological_sort()  # Fail early if graph is acyclic
        self._steps_cache = SimpleCache()
        self._get_required_steps(self._internal_inputs, self._internal_outputs)

    def _get_required_steps(self, inputs: Sequence[DataPlaceholder], outputs: Sequence[DataPlaceholder]) -> List[Step]:
        # Backtrack from outputs until inputs to get the necessary steps. That is,
        # find the ancestors of the nodes that provide the specified outputs.
        # Raise an error if there is an ancestor whose input is not in the specified inputs.
        # We assume a DAG (guaranteed by success of topological_sort).
        cache_key = (tuple(sorted(inputs)), tuple(sorted(outputs)))
        if cache_key in self._steps_cache:
            return self._steps_cache[cache_key]

        required_steps = set()
        inputs_found = []

        # Depth-first search
        def backtrack(output):
            steps_required_by_output = set()

            if output in inputs:
                inputs_found.append(output)
                return steps_required_by_output

            parent_step = output.step
            if parent_step in required_steps:
                return steps_required_by_output

            steps_required_by_output = {parent_step}
            for input in parent_step.inputs:
                steps_required_by_output |= backtrack(input)
            return steps_required_by_output

        for output in outputs:
            required_steps |= backtrack(output)

        # Check for missing inputs
        missing_inputs = []
        for step in required_steps:
            if self._graph.in_degree(step) == 0:
                missing_inputs.extend(step.outputs)

        if missing_inputs:
            raise RuntimeError('The following inputs are required but were not specified:\n'
                               '{}'.format(','.join([input.name for input in missing_inputs])))

        # Check for any unused inputs
        for input in inputs:
            if input not in inputs_found:
                raise RuntimeError('Input {} was provided but it is not required '
                                   'to compute the specified outputs.'.format(input.name))

        required_steps = [step for step in self._all_steps_sorted if step in required_steps]
        self._steps_cache[cache_key] = required_steps

        return required_steps

    def _normalize_data(self,
                        data: Union[ArrayLike, List[ArrayLike], Dict[DataPlaceholder, ArrayLike], Dict[str, ArrayLike]],
                        data_placeholders: List[DataPlaceholder],
                        expand_none=False) -> Dict[DataPlaceholder, ArrayLike]:
        if isinstance(data, dict):
            return self._normalize_dict(data)
        else:
            return self._normalize_list(data, data_placeholders, expand_none)

    def _normalize_dict(self, data: Union[Dict[DataPlaceholder, ArrayLike], Dict[str, ArrayLike]]) -> Dict[DataPlaceholder, ArrayLike]:
        data_norm = {}
        for key, value in data.items():
            key = self.get_data_placeholder(key.name if isinstance(key, DataPlaceholder) else key)
            data_norm[key] = value
        return data_norm

    @staticmethod
    def _normalize_list(data: Union[ArrayLike, List[ArrayLike]],
                        data_placeholders: List[DataPlaceholder],
                        expand_none) -> Dict[DataPlaceholder, ArrayLike]:
        if data is None and expand_none:
            data = [None] * len(data_placeholders)
        else:
            data = listify(data)

        try:
            data_norm = dict(safezip2(data_placeholders, data))

        except ValueError as e:
            # TODO: Improve this message
            message = 'When passing inputs/outputs as a list or a single array, ' \
                      'the number of arrays must match the number of inputs/outputs ' \
                      'specified at instantiation. ' \
                      'Got {}, expected: {}.'.format(len(data), len(data_placeholders))
            raise ValueError(message) from e

        return data_norm

    def get_step(self, name: str) -> Step:
        # Steps are assumed to have unique names (guaranteed by success of _build_graph)
        if name in self._steps.keys():
            return self._steps[name]
        raise ValueError('{} was not found in the model.'.format(name))

    def get_data_placeholder(self, name: str) -> DataPlaceholder:
        # If the step names are unique, so are the data_placeholder names
        if name in self._data_placeholders.keys():
            return self._data_placeholders[name]
        raise ValueError('{} was not found in the model.'.format(name))

    def fit(self, X, y=None, extra_targets=None, **fit_params):
        # TODO: Add better error message to know which step failed in case of any error
        # TODO: Consider using joblib's Parallel and Memory classes to parallelize and cache computations
        # In graph parlance, the 'parallelizable' paths of a graph are called 'disjoint paths'
        # https://stackoverflow.com/questions/37633941/get-list-of-parallel-paths-in-a-directed-graph

        # input/output normalization
        X = self._normalize_data(X, self._internal_inputs)
        for input in self._internal_inputs:
            if input not in X:
                raise ValueError('Missing input {}.'.format(input))

        y = self._normalize_data(y, self._internal_outputs, expand_none=True)
        for output in self._internal_outputs:
            if output not in y:
                raise ValueError('Missing output {}.'.format(output))

        if extra_targets is not None:
            y.update(self._normalize_dict(extra_targets))

        # Get steps and their fit_params
        steps = self._get_required_steps(X, y)
        fit_params_steps = defaultdict(dict)
        for param_key, param_value in fit_params.items():
            # TODO: Add check for __. Add error message if step was not found
            step_name, _, param_name = param_key.partition('__')
            step = self.get_step(step_name)
            fit_params_steps[step][param_name] = param_value

        # Intermediate results are stored here
        # keys: DataPlaceholder instances, values: actual data (e.g. numpy arrays)
        results_cache = dict()
        results_cache.update(X)

        for step in steps:
            Xs = [results_cache[i] for i in step.inputs]

            # TODO: Use fit_transform if step has it
            # 1) Fit phase
            if hasattr(step, 'fit') and step.trainable:
                # Filtering out None y's allow us to define fit methods without y=None.
                ys = [y[o] for o in step.outputs
                      if o in y and y[o] is not None]

                fit_params = fit_params_steps.get(step, {})

                # TODO: Add a try/except to catch missing output data errors (e.g. when forgot ensemble outputs)
                step.fit(*Xs, *ys, **fit_params)

            # 2) predict/transform phase
            self._compute_step(step, Xs, results_cache)

        return self

    def predict(self, X, outputs=None):
        # TODO: Make outputs Optional[Union[str, List[str], DataPlaceholder, List[DataPlaceholder]]]

        # Intermediate results are stored here
        # keys: DataPlaceholder instances, values: actual data (e.g. numpy arrays)
        results_cache = dict()

        # Normalize inputs and outputs
        X = self._normalize_data(X, self._internal_inputs)

        if outputs is None:
            outputs = self._internal_outputs
        else:
            outputs = listify(outputs)
            if len(set(outputs)) != len(outputs):
                raise ValueError('outputs must be unique.')
            outputs = [self.get_data_placeholder(output) for output in outputs]

        steps = self._get_required_steps(X, outputs)

        # Compute
        results_cache.update(X)

        for step in steps:
            Xs = [results_cache[i] for i in step.inputs]
            self._compute_step(step, Xs, results_cache)

        output_data = [results_cache[o] for o in outputs]
        if len(output_data) == 1:
            return output_data[0]
        else:
            return output_data

    @staticmethod
    def _compute_step(step, Xs, cache):
        # TODO: Raise warning if computed output is already in cache.
        # This happens when recomputing a step that had a subset of its outputs already passed in the inputs.
        # TODO: Some regressors have extra options in their predict method, and they return a tuple of arrays.
        # https://scikit-learn.org/stable/glossary.html#term-predict
        output_data = step.compute(*Xs)
        output_data = listify(output_data)

        try:
            cache.update(safezip2(step.outputs, output_data))
        except ValueError as e:
            message = 'The number of output data elements ({}) does not match ' \
                      'the number of {} outputs ({}).'.format(len(output_data), step.name, len(step.outputs))
            raise RuntimeError(message) from e

    def get_params(self, deep=True):
        # InputSteps are excluded
        params = {}
        for step in self._steps.values():
            if isinstance(step, InputStep):
                continue
            params[step.name] = step
            if hasattr(step, 'get_params'):
                for param_name, value in step.get_params(deep).items():
                    params['{}__{}'.format(step.name, param_name)] = value
        return params

    def set_params(self, **params):
        # ----- 1. Replace steps
        for key in list(params.keys()):
            if key in self._steps:
                self._replace_step(key, params.pop(key))

        # ----- 2. Replace each step params
        # Collect params by step
        step_params = defaultdict(dict)
        for key, value in params.items():
            step_name, _, param_name = key.partition('__')
            step_params[step_name][param_name] = value

        # Set params for each step
        for step_name, params in step_params.items():
            step = self.get_step(step_name)
            step.set_params(**params)

        return self

    def _replace_step(self, step_key, new_step):
        # Transfer connectivity configuration from old step
        # to new step and replace old with new
        transfer_attrs = ['name', 'trainable', 'inputs', 'outputs']
        old_step = self._steps[step_key]
        for attr in transfer_attrs:
            setattr(new_step, attr, getattr(old_step, attr))

        # Update outputs of old step to point to the new step
        for output in old_step.outputs:
            output.step = new_step

        # Rebuild model
        self._build()

    @property
    def graph(self):
        return self._graph


def build_graph_from_outputs(outputs):
    graph = DiGraph()

    # Add nodes (steps)
    def collect_steps_from(output):
        parent_step = output.step

        if parent_step in graph:
            return

        graph.add_node(parent_step)
        for input in parent_step.inputs:
            collect_steps_from(input)

    for output in outputs:
        collect_steps_from(output)

    # Add edges (data)
    for step in graph:
        for input in step.inputs:
            graph.add_edge(input.step, step, input)

    # Check for any nodes (steps) with duplicated names
    duplicated_names = find_duplicated_items([step.name for step in graph])

    if duplicated_names:
        raise RuntimeError('A graph cannot contain steps with duplicated names. '
                           'Found the following duplicates:\n'
                           '{}'.format(duplicated_names))

    return graph