"""Tests for jobs module."""

import pytest

from jobs import ReverseLookupDict, get_component_status_info


class TestReverseLookupDict:
    """Tests for ReverseLookupDict class."""
    
    def test_basic_usage(self):
        """Should work as a defaultdict(list)."""
        rld = ReverseLookupDict()
        rld['key1'].append('value1')
        rld['key1'].append('value2')
        rld['key2'].append('value3')
        
        assert rld['key1'] == ['value1', 'value2']
        assert rld['key2'] == ['value3']
    
    def test_key_lookup(self):
        """Should be able to reverse-lookup keys from values."""
        rld = ReverseLookupDict()
        rld['component1'].append('pkg1')
        rld['component1'].append('pkg2')
        rld['component2'].append('pkg3')
        
        assert rld.key('pkg1') == 'component1'
        assert rld.key('pkg2') == 'component1'
        assert rld.key('pkg3') == 'component2'
    
    def test_key_lookup_caches(self):
        """Should cache reverse lookups."""
        rld = ReverseLookupDict()
        rld['key1'].append('value1')
        
        # First lookup
        result1 = rld.key('value1')
        # Second lookup should use cache
        result2 = rld.key('value1')
        
        assert result1 == result2 == 'key1'
        assert 'value1' in rld._reverse_lookup_cache
    
    def test_key_lookup_not_found(self):
        """Should raise KeyError for values not in any list."""
        rld = ReverseLookupDict()
        rld['key1'].append('value1')
        
        with pytest.raises(KeyError, match="Value 'nonexistent' found in no list"):
            rld.key('nonexistent')
    
    def test_all_values(self):
        """Should return set of all values across all keys."""
        rld = ReverseLookupDict()
        rld['key1'].append('value1')
        rld['key1'].append('value2')
        rld['key2'].append('value3')
        rld['key2'].append('value4')
        
        all_vals = rld.all_values()
        assert all_vals == {'value1', 'value2', 'value3', 'value4'}
    
    def test_all_values_empty(self):
        """Should return empty set for empty dict."""
        rld = ReverseLookupDict()
        assert rld.all_values() == set()
    
    def test_all_values_deduplicates(self):
        """Should return unique values even if duplicated across keys."""
        rld = ReverseLookupDict()
        rld['key1'].append('value1')
        rld['key2'].append('value1')  # Duplicate
        rld['key3'].append('value2')
        
        all_vals = rld.all_values()
        assert all_vals == {'value1', 'value2'}
    
    def test_default_factory_can_be_disabled(self):
        """Should be able to disable auto-creation of lists."""
        rld = ReverseLookupDict()
        rld['key1'].append('value1')
        
        # Disable default factory
        rld.default_factory = None
        
        # Now accessing non-existent key should raise KeyError
        with pytest.raises(KeyError):
            _ = rld['nonexistent']
    
    def test_multiple_values_per_key(self):
        """Should handle multiple values per key."""
        rld = ReverseLookupDict()
        values = ['v1', 'v2', 'v3', 'v4', 'v5']
        for val in values:
            rld['key1'].append(val)
        
        assert rld['key1'] == values
        for val in values:
            assert rld.key(val) == 'key1'


class TestGetComponentStatusInfo:
    """Tests for get_component_status_info function."""
    
    def test_component_blocked_with_missing_packages(self):
        """Should show blocked status with missing packages."""
        missing_packages = {'comp1': {'pkg1', 'pkg2', 'pkg3'}}
        components = {'comp1': ['some_pkg']}
        
        status = get_component_status_info('comp1', missing_packages, components)
        assert '(blocked by:' in status
        # Should show first 3 packages
        assert any(pkg in status for pkg in ['pkg1', 'pkg2', 'pkg3'])
    
    def test_component_blocked_with_many_packages(self):
        """Should truncate and add ... for many missing packages."""
        missing_packages = {'comp1': {f'pkg{i}' for i in range(10)}}
        components = {'comp1': ['some_pkg']}
        
        status = get_component_status_info('comp1', missing_packages, components)
        assert '(blocked by:' in status
        assert '...' in status
    
    def test_component_blocked_unknown_reason(self):
        """Should show unknown reason if in missing_packages but empty."""
        missing_packages = {'comp1': set()}
        components = {'comp1': ['some_pkg']}
        
        status = get_component_status_info('comp1', missing_packages, components)
        assert '(blocked for unknown reason)' in status
    
    def test_component_ready(self):
        """Should show ready status if in components but not in missing_packages."""
        missing_packages = {}
        components = {'comp1': ['some_pkg']}
        
        status = get_component_status_info('comp1', missing_packages, components)
        assert '(ready)' in status
    
    def test_component_build_failed(self):
        """Should show build failed if not in components."""
        missing_packages = {}
        components = {}
        
        status = get_component_status_info('comp1', missing_packages, components)
        assert '(build failed)' in status
    
    def test_blocked_with_few_packages(self):
        """Should show all packages if 3 or fewer."""
        missing_packages = {'comp1': {'pkg1', 'pkg2'}}
        components = {'comp1': ['some_pkg']}
        
        status = get_component_status_info('comp1', missing_packages, components)
        assert 'pkg1' in status
        assert 'pkg2' in status
        assert '...' not in status
    
    def test_blocked_with_exactly_three_packages(self):
        """Should show all 3 packages without truncation."""
        missing_packages = {'comp1': {'pkg1', 'pkg2', 'pkg3'}}
        components = {'comp1': ['some_pkg']}
        
        status = get_component_status_info('comp1', missing_packages, components)
        assert '...' not in status
    
    def test_blocked_with_four_packages(self):
        """Should truncate at 4 packages."""
        missing_packages = {'comp1': {'pkg1', 'pkg2', 'pkg3', 'pkg4'}}
        components = {'comp1': ['some_pkg']}
        
        status = get_component_status_info('comp1', missing_packages, components)
        assert '...' in status

