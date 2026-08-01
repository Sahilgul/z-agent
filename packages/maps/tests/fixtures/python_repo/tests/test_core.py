"""Tests for the core service."""

from python_repo.pkg.core import helper


def test_helper_adds():
    assert helper(1, 2) == 3


def test_helper_zero():
    assert helper(0, 0) == 0
