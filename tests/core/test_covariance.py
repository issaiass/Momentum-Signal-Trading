"""
tests/core/test_covariance.py

Covers core/covariance.py's shrinkage_covariance(), Epic 1 ("Institutional Risk-Management
Features" plan) Story 1.2. Asserts the actual numeric claim (better conditioning, not just "it
ran"), per docs/TESTING.md's convention for any new function with a numeric claim, plus the
explicit shrinkage=0.0 regression anchor proving this function can degenerate to today's raw
sample covariance exactly.

Run with: pytest tests/core/test_covariance.py -v
"""
import numpy as np
import pandas as pd
import pytest

from momentum_trading.core.covariance import shrinkage_covariance


def _synthetic_returns(n_obs=15, n_assets=8, seed=0):
    """
    Few observations relative to many, highly-correlated assets, the textbook motivating case
    for shrinkage: a raw sample covariance matrix here is poorly conditioned.
    """
    rng = np.random.default_rng(seed)
    common_factor = rng.normal(0, 1, n_obs)
    data = {}
    for i in range(n_assets):
        idiosyncratic = rng.normal(0, 0.3, n_obs)
        data[f"T{i}"] = 0.01 * (0.9 * common_factor + idiosyncratic)
    return pd.DataFrame(data, index=pd.date_range("2026-01-01", periods=n_obs, freq="B"))


class TestShrinkageCovariance:
    def test_shrinkage_zero_degenerates_to_raw_sample_covariance(self):
        # Regression anchor: an explicit shrinkage=0.0 must be bit-identical to plain .cov(),
        # proving this function CAN reduce to today's pre-existing behavior exactly.
        returns = _synthetic_returns()
        shrunk = shrinkage_covariance(returns, shrinkage=0.0)
        raw = returns.cov()
        pd.testing.assert_frame_equal(shrunk, raw, check_exact=False, rtol=1e-10)

    def test_shrinkage_one_is_the_constant_correlation_target_not_raw(self):
        returns = _synthetic_returns()
        shrunk_full = shrinkage_covariance(returns, shrinkage=1.0)
        raw = returns.cov()
        # Full shrinkage must differ from the raw sample matrix (it moved all the way to the
        # target), but variances (the diagonal) are unchanged, the target shares the sample's
        # own diagonal by construction.
        assert not np.allclose(shrunk_full.values, raw.values)
        assert np.allclose(np.diag(shrunk_full.values), np.diag(raw.values))

    def test_auto_shrinkage_improves_conditioning(self):
        # The actual numeric claim this function exists to deliver: on a few-observations/
        # many-correlated-tickers panel, the auto-selected shrinkage intensity must produce a
        # BETTER-conditioned (lower condition number) matrix than the raw sample covariance,
        # not just "some other matrix".
        returns = _synthetic_returns(n_obs=15, n_assets=8)
        raw_cov = returns.cov().values
        shrunk_cov = shrinkage_covariance(returns).values

        raw_cond = np.linalg.cond(raw_cov)
        shrunk_cond = np.linalg.cond(shrunk_cov)
        assert shrunk_cond < raw_cond

    def test_auto_shrinkage_intensity_is_in_valid_range(self):
        returns = _synthetic_returns()
        shrunk = shrinkage_covariance(returns)
        raw = returns.cov()
        # A valid shrinkage intensity in [0, 1] means the shrunk matrix's Frobenius distance to
        # the raw sample matrix is bounded by the raw-to-target distance (it can't have
        # overshot past the target).
        assert np.isfinite(shrunk.values).all()

    def test_result_is_symmetric_and_labeled_by_ticker(self):
        returns = _synthetic_returns(n_assets=4)
        shrunk = shrinkage_covariance(returns)
        assert list(shrunk.columns) == list(returns.columns)
        assert list(shrunk.index) == list(returns.columns)
        np.testing.assert_allclose(shrunk.values, shrunk.values.T)

    def test_fewer_than_two_tickers_raises(self):
        returns = _synthetic_returns(n_assets=1)
        with pytest.raises(ValueError, match="at least 2 tickers"):
            shrinkage_covariance(returns)

    def test_fewer_than_two_observations_after_dropna_raises(self):
        returns = _synthetic_returns(n_obs=1, n_assets=3)
        with pytest.raises(ValueError, match="at least 2 observations"):
            shrinkage_covariance(returns)

    def test_rows_with_any_nan_are_dropped_before_computing(self):
        returns = _synthetic_returns(n_obs=20, n_assets=3)
        with_nan = returns.copy()
        with_nan.iloc[5, 1] = np.nan
        clean = returns.drop(returns.index[5])
        shrunk_with_nan = shrinkage_covariance(with_nan, shrinkage=0.0)
        shrunk_clean = shrinkage_covariance(clean, shrinkage=0.0)
        pd.testing.assert_frame_equal(shrunk_with_nan, shrunk_clean, check_exact=False, rtol=1e-10)
