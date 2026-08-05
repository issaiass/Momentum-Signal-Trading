"""
core/covariance.py

Pure covariance-matrix utilities, no execution or I/O side effects, per core/'s architecture
rule (same "one file per pure-numerical-domain" precedent as core/technical_indicators.py).

Epic 1, "Institutional Risk-Management Features" plan, Story 1.2: this project's existing
correlation-based mechanisms (backtest/momentum_backtest.py's detect_correlation_spike() and
_correlation_penalty_weights()) both compute their correlation matrix via raw
pandas.DataFrame.corr()/.cov(), with no regularization at all, confirmed by reading both
directly. Raw sample covariance is poorly conditioned exactly when the number of return
observations is small relative to the number of tickers, the textbook motivating case for
shrinkage estimation, and exactly the situation a risk-parity/Equal Risk Contribution sizing
solve (Story 1.3) needs a well-conditioned matrix for.

New, unwired utility: nothing in this project calls shrinkage_covariance() by default. It is
opted into per call site (backtest/momentum_backtest.py's use_shrinkage_covariance flag, and
directly by Story 1.3's ERC sizing function), zero behavior change to any existing caller.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def shrinkage_covariance(returns: pd.DataFrame, shrinkage: float | None = None) -> pd.DataFrame:
    """
    Ledoit-Wolf-style shrinkage covariance estimator: shrinks the raw sample covariance matrix
    toward a constant-correlation target (Ledoit & Wolf 2004's target choice), better
    conditioned than the raw sample matrix when the number of observations is small relative to
    the number of tickers.

    shrinkage=None (default) computes a practical analytic shrinkage intensity via
    _estimate_shrinkage_intensity() below, a simplified variant of Ledoit & Wolf's original
    formula (their full asymptotic derivation also nets out a covariance-of-estimation-errors
    cross term this omits for tractability) -- sufficient for conditioning a covariance matrix
    for portfolio construction, not presented as a research-grade replication of the paper.
    Pass an explicit float in [0, 1] to override; 0.0 degenerates to the raw sample covariance
    exactly (the regression anchor proving this function CAN reduce to today's un-shrunk
    behavior).

    returns: periodic (not cumulative) returns, columns = tickers, rows = dates, e.g.
    daily_prices[tickers].pct_change(). Rows containing any NaN are dropped before computing
    (same "align on complete observations" convention _correlation_penalty_weights() already
    uses via its own window.corr() call).
    """
    if returns.shape[1] < 2:
        raise ValueError("shrinkage_covariance requires at least 2 tickers (columns)")
    x = returns.dropna(how="any")
    if len(x) < 2:
        raise ValueError(
            "shrinkage_covariance requires at least 2 observations (rows) after dropping NaN"
        )

    tickers = x.columns
    n_obs = len(x)
    n_assets = len(tickers)

    sample_cov = x.cov().values
    variances = np.diag(sample_cov)
    std = np.sqrt(np.clip(variances, 0.0, None))

    outer_std = np.outer(std, std)
    with np.errstate(invalid="ignore", divide="ignore"):
        corr = np.where(outer_std > 0, sample_cov / outer_std, 0.0)
    np.fill_diagonal(corr, 1.0)

    off_diag_mask = ~np.eye(n_assets, dtype=bool)
    avg_corr = corr[off_diag_mask].mean() if n_assets > 1 else 0.0

    target = avg_corr * outer_std
    np.fill_diagonal(target, variances)

    if shrinkage is None:
        shrinkage = _estimate_shrinkage_intensity(x.values, sample_cov, target, n_obs)
    shrinkage = float(np.clip(shrinkage, 0.0, 1.0))

    shrunk = shrinkage * target + (1 - shrinkage) * sample_cov
    return pd.DataFrame(shrunk, index=tickers, columns=tickers)


def _estimate_shrinkage_intensity(
    x: np.ndarray, sample_cov: np.ndarray, target: np.ndarray, n_obs: int
) -> float:
    """
    Practical analytic shrinkage intensity: the ratio of the sample covariance matrix's own
    estimated sampling-error variance to its squared distance from the shrinkage target, clipped
    to [0, 1]. This is the core idea of Ledoit & Wolf (2004)'s optimal shrinkage intensity,
    simplified (their exact formula also subtracts a covariance-of-estimation-errors term
    between the sample matrix and the target, omitted here).
    """
    demeaned = x - x.mean(axis=0, keepdims=True)
    n_assets = sample_cov.shape[0]
    pi_hat = 0.0
    for i in range(n_assets):
        for j in range(n_assets):
            prod = demeaned[:, i] * demeaned[:, j]
            pi_hat += np.mean((prod - sample_cov[i, j]) ** 2) / n_obs
    gamma_hat = np.sum((sample_cov - target) ** 2)
    if gamma_hat <= 0:
        return 0.0
    return float(np.clip(pi_hat / gamma_hat, 0.0, 1.0))
