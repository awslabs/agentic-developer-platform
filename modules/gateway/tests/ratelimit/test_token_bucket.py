"""
Unit tests for the token bucket algorithm.

This module tests the TokenBucket class for correct behavior including:
- Token consumption
- Refill logic
- Concurrent access
- Edge cases
"""

import threading
import time

import pytest

from src.ratelimit.token_bucket import BucketState, TokenBucket, TokenBucketFactory


class TestTokenBucket:
    """Tests for TokenBucket class."""

    def test_init_valid_parameters(self):
        """Test initialization with valid parameters."""
        bucket = TokenBucket(capacity=100.0, refill_rate=10.0)
        assert bucket.capacity == 100.0
        assert bucket.refill_rate == 10.0
        assert bucket.tokens == 100.0

    def test_init_invalid_capacity(self):
        """Test initialization with invalid capacity."""
        with pytest.raises(ValueError, match="Capacity must be positive"):
            TokenBucket(capacity=0, refill_rate=10.0)

        with pytest.raises(ValueError, match="Capacity must be positive"):
            TokenBucket(capacity=-10, refill_rate=10.0)

    def test_init_invalid_refill_rate(self):
        """Test initialization with negative refill rate."""
        with pytest.raises(ValueError, match="Refill rate cannot be negative"):
            TokenBucket(capacity=100, refill_rate=-1.0)

    def test_init_zero_refill_rate(self):
        """Test initialization with zero refill rate is valid."""
        bucket = TokenBucket(capacity=100.0, refill_rate=0.0)
        assert bucket.refill_rate == 0.0

    def test_consume_success(self):
        """Test successful token consumption."""
        bucket = TokenBucket(capacity=100.0, refill_rate=10.0)
        assert bucket.consume(10) is True
        assert bucket.tokens == 90.0

    def test_consume_failure_not_enough_tokens(self):
        """Test consumption fails when not enough tokens."""
        bucket = TokenBucket(capacity=10.0, refill_rate=1.0)
        assert bucket.consume(15) is False
        assert bucket.tokens == 10.0  # No tokens consumed

    def test_consume_invalid_tokens(self):
        """Test consumption with invalid token amount."""
        bucket = TokenBucket(capacity=100.0, refill_rate=10.0)
        with pytest.raises(ValueError, match="Tokens to consume must be positive"):
            bucket.consume(0)

        with pytest.raises(ValueError, match="Tokens to consume must be positive"):
            bucket.consume(-5)

    def test_consume_exact_tokens(self):
        """Test consuming exactly available tokens."""
        bucket = TokenBucket(capacity=10.0, refill_rate=0.0)
        assert bucket.consume(10) is True
        assert bucket.tokens == 0.0

    def test_try_consume_success(self):
        """Test try_consume returns correct tuple on success."""
        bucket = TokenBucket(capacity=100.0, refill_rate=10.0)
        success, remaining, wait_time = bucket.try_consume(10)
        assert success is True
        assert remaining == 90.0
        assert wait_time == 0.0

    def test_try_consume_failure(self):
        """Test try_consume returns correct tuple on failure."""
        bucket = TokenBucket(capacity=10.0, refill_rate=1.0)
        success, remaining, wait_time = bucket.try_consume(15)
        assert success is False
        assert remaining == 10.0
        assert wait_time == pytest.approx(5.0, rel=0.1)

    def test_try_consume_failure_zero_refill(self):
        """Test try_consume with zero refill rate returns infinite wait."""
        bucket = TokenBucket(capacity=10.0, refill_rate=0.0)
        success, remaining, wait_time = bucket.try_consume(15)
        assert success is False
        assert wait_time == float("inf")

    def test_check_without_consuming(self):
        """Test check method doesn't consume tokens."""
        bucket = TokenBucket(capacity=100.0, refill_rate=10.0)
        allowed, tokens, wait_time = bucket.check(10)
        assert allowed is True
        assert tokens == 100.0  # No tokens consumed
        assert wait_time == 0.0

    def test_check_would_fail(self):
        """Test check reports failure correctly."""
        bucket = TokenBucket(capacity=10.0, refill_rate=1.0)
        allowed, tokens, wait_time = bucket.check(15)
        assert allowed is False
        assert tokens == 10.0
        assert wait_time == pytest.approx(5.0, rel=0.1)

    def test_refill_over_time(self):
        """Test tokens refill over time."""
        bucket = TokenBucket(capacity=100.0, refill_rate=100.0)  # 100 tokens/sec
        bucket.consume(50)  # Use 50 tokens
        assert bucket.tokens == 50.0

        time.sleep(0.2)  # Wait 200ms, should refill ~20 tokens
        remaining = bucket.get_remaining()
        assert remaining >= 60.0  # Should have refilled some tokens
        assert remaining <= 100.0  # But not exceed capacity

    def test_refill_does_not_exceed_capacity(self):
        """Test refill doesn't exceed capacity."""
        bucket = TokenBucket(capacity=100.0, refill_rate=1000.0)  # Very fast refill
        bucket.consume(10)
        time.sleep(0.1)  # Wait for refill
        assert bucket.get_remaining() == 100.0  # Capped at capacity

    def test_get_remaining(self):
        """Test get_remaining returns correct value."""
        bucket = TokenBucket(capacity=100.0, refill_rate=0.0)  # No refill for precise test
        assert bucket.get_remaining() == 100.0

        bucket.consume(30)
        assert bucket.get_remaining() == 70.0

    def test_reset(self):
        """Test reset restores bucket to full capacity."""
        bucket = TokenBucket(capacity=100.0, refill_rate=10.0)
        bucket.consume(80)
        assert bucket.tokens == 20.0

        bucket.reset()
        assert bucket.tokens == 100.0

    def test_get_state(self):
        """Test get_state returns BucketState."""
        bucket = TokenBucket(capacity=100.0, refill_rate=0.0)  # No refill for precise test
        bucket.consume(30)

        state = bucket.get_state()
        assert isinstance(state, BucketState)
        assert state.tokens == 70.0

    def test_concurrent_access(self):
        """Test thread-safe concurrent access."""
        bucket = TokenBucket(capacity=1000.0, refill_rate=0.0)  # No refill
        results = []
        errors = []

        def consume_tokens():
            try:
                for _ in range(100):
                    results.append(bucket.consume(1))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=consume_tokens) for _ in range(10)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert results.count(True) == 1000  # All 1000 tokens consumed
        assert results.count(False) == 0
        assert bucket.tokens == 0.0


class TestTokenBucketFactory:
    """Tests for TokenBucketFactory class."""

    def test_create_rpm_bucket(self):
        """Test creating RPM bucket."""
        bucket = TokenBucketFactory.create_rpm_bucket(60)  # 60 RPM
        assert bucket.capacity >= 1
        assert bucket.refill_rate == pytest.approx(1.0, rel=0.01)  # 60/60 = 1/sec

    def test_create_rpm_bucket_with_burst(self):
        """Test creating RPM bucket with burst multiplier."""
        bucket = TokenBucketFactory.create_rpm_bucket(60, burst_multiplier=2.0)
        assert bucket.capacity >= 1

    def test_create_tpm_bucket(self):
        """Test creating TPM bucket."""
        bucket = TokenBucketFactory.create_tpm_bucket(60000)  # 60000 TPM
        assert bucket.capacity >= 1
        assert bucket.refill_rate == pytest.approx(1000.0, rel=0.01)  # 60000/60 = 1000/sec

    def test_create_tpm_bucket_with_burst(self):
        """Test creating TPM bucket with burst multiplier."""
        bucket = TokenBucketFactory.create_tpm_bucket(60000, burst_multiplier=1.5)
        assert bucket.capacity >= 1


class TestTokenBucketEdgeCases:
    """Edge case tests for TokenBucket."""

    def test_fractional_tokens(self):
        """Test bucket handles fractional tokens."""
        bucket = TokenBucket(capacity=1.5, refill_rate=0.0)  # No refill for precise test
        assert bucket.consume(0.5) is True
        assert bucket.tokens == 1.0
        assert bucket.consume(0.5) is True
        assert bucket.tokens == 0.5

    def test_very_small_refill_rate(self):
        """Test bucket handles very small refill rates."""
        bucket = TokenBucket(capacity=100.0, refill_rate=0.001)
        bucket.consume(50)
        time.sleep(0.1)
        remaining = bucket.get_remaining()
        assert remaining >= 50.0  # Should have some refill
        assert remaining < 51.0  # But very little

    def test_empty_bucket_refill(self):
        """Test empty bucket refills correctly."""
        bucket = TokenBucket(capacity=10.0, refill_rate=1000.0)  # Very fast refill
        for _ in range(10):
            bucket.consume(1)
        assert bucket.tokens == pytest.approx(0.0, abs=0.5)  # May have minor refill

        time.sleep(0.1)  # Wait for refill
        remaining = bucket.get_remaining()
        assert remaining >= 9.0  # Should be nearly full
        assert remaining <= 10.0

    def test_burst_consumption(self):
        """Test consuming all tokens at once (burst)."""
        bucket = TokenBucket(capacity=100.0, refill_rate=10.0)
        assert bucket.consume(100) is True
        assert bucket.tokens == 0.0
        assert bucket.consume(1) is False  # Can't consume anymore
