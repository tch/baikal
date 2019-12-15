from contextlib import contextmanager
from functools import partial

import pytest
from baikal._core.step import InputStep

from baikal import Input, Step
from baikal._core.data_placeholder import DataPlaceholder

from tests.helpers.fixtures import teardown
from tests.helpers.dummy_steps import DummyMIMO, DummySISO, DummyEstimator
from tests.helpers.sklearn_steps import LogisticRegression, PCA


@contextmanager
def does_not_raise():
    yield


class TestInput:
    def test_instantiation(self, teardown):
        x0 = Input()

        assert isinstance(x0, DataPlaceholder)
        assert "InputStep_0" == x0.name

    def test_instantiate_two_without_name(self, teardown):
        x0 = Input()
        x1 = Input()

        assert "InputStep_0" == x0.name
        assert "InputStep_1" == x1.name


class TestInputStep:
    def test_repr(self):
        step = InputStep(name="x1")
        assert "InputStep(name='x1')" == repr(step)


class TestStep:
    def test_instantiate_two_without_name(self, teardown):
        lr0 = LogisticRegression()
        lr1 = LogisticRegression()

        assert "LogisticRegression_0" == lr0.name
        assert "LogisticRegression_1" == lr1.name

    def test_instantiate_with_invalid_function_argument(self):
        class DummyStep(Step):
            def somefunc(self, X):
                pass

        class DummyStepWithPredict(Step):
            def predict(self, X):
                pass

        class DummyStepWithTransform(Step):
            def transform(self, X):
                pass

        with pytest.raises(ValueError):
            DummyStep(function=None)

        step = DummyStep(function="somefunc")
        assert step.function == step.somefunc

        def anotherfunc():
            pass

        step = DummyStep(function=anotherfunc)
        assert step.function == anotherfunc

        step = DummyStepWithPredict()
        assert step.function == step.predict

        step = DummyStepWithTransform()
        assert step.function == step.transform

    # Below tests are parametrized to take two kind of fittable steps:
    # - step that requires y (e.g. Logistic Regression)
    # - step that does not require y (e.g. PCA)

    @pytest.mark.parametrize("step_class", [LogisticRegression, PCA])
    @pytest.mark.parametrize("trainable", [True, False])
    def test_call_without_targets(self, step_class, trainable, teardown):
        x = Input()
        step_class(trainable=trainable)(x)

    @pytest.mark.parametrize("step_class", [LogisticRegression, PCA])
    @pytest.mark.parametrize(
        "trainable,expectation",
        [(True, does_not_raise), (False, partial(pytest.warns, UserWarning))],
    )
    def test_call_with_targets(self, step_class, trainable, expectation, teardown):
        x = Input()
        y_t = Input()
        with expectation():
            step_class(trainable=trainable)(x, y_t)

    def test_call_without_targets_without_fit_method(self, teardown):
        x = Input()
        DummySISO()(x)

    def test_call_with_targets_without_fit_method(self, teardown):
        x = Input()
        y_t = Input()
        with pytest.raises(RuntimeError):
            DummySISO()(x, y_t)

    def test_call_with_two_inputs(self, teardown):
        x0 = Input()
        x1 = Input()
        y0, y1 = DummyMIMO()([x0, x1])

        assert isinstance(y0, DataPlaceholder)
        assert isinstance(y1, DataPlaceholder)
        assert "DummyMIMO_0/0" == y0.name
        assert "DummyMIMO_0/1" == y1.name

    def test_repr(self):
        class DummyStep(Step):
            def somefunc(self, X):
                pass

        step = DummyStep(name="some-step", function="somefunc")
        assert (
            "DummyStep(name='some-step', function='somefunc', "
            "n_outputs=1, trainable=True)" == repr(step)
        )

        # TODO: Add test for sklearn step

    def test_get_params(self, teardown):
        step = DummyEstimator()
        params = step.get_params()
        expected = {"x": 123, "y": "abc"}
        assert expected == params

    def test_set_params(self, teardown):
        step = DummyEstimator()

        new_params_wrong = {"non_existent_param": 42}
        with pytest.raises(ValueError):
            step.set_params(**new_params_wrong)

        new_params = {"x": 456}
        step.set_params(**new_params)
        params = step.get_params()
        expected = {"x": 456, "y": "abc"}
        assert expected == params
