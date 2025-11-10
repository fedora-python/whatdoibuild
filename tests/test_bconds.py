"""Tests for bconds module."""

import datetime
import pathlib
from unittest.mock import Mock, patch
import pytest

from bconds import bcond_cache_identifier, koji_id_is_older_than_week


class TestBcondCacheIdentifier:
    """Tests for bcond_cache_identifier function."""
    
    def test_simple_component_no_bconds(self):
        """Should generate identifier for component with no bconds."""
        identifier = bcond_cache_identifier('mypackage', {})
        assert identifier.startswith('mypackage:')
        assert identifier == 'mypackage:::::'
    
    def test_with_withouts_only(self):
        """Should include withouts in identifier."""
        config = {'withouts': ['tests', 'docs']}
        identifier = bcond_cache_identifier('pkg', config)
        assert identifier == 'pkg:docs-tests::::'
    
    def test_with_withs_only(self):
        """Should include withs in identifier."""
        config = {'withs': ['bootstrap', 'feature']}
        identifier = bcond_cache_identifier('pkg', config)
        assert identifier == 'pkg::bootstrap-feature:::'
    
    def test_with_replacements_only(self):
        """Should include replacement macro names in identifier."""
        config = {'replacements': {'macro1': 'value1', 'macro2': 'value2'}}
        identifier = bcond_cache_identifier('pkg', config)
        assert identifier == 'pkg:::macro1-macro2::'
    
    def test_complete_config(self):
        """Should include all parts in identifier."""
        config = {
            'withouts': ['tests', 'docs'],
            'withs': ['bootstrap'],
            'replacements': {'debug': '0'}
        }
        identifier = bcond_cache_identifier('pkg', config)
        assert identifier == 'pkg:docs-tests:bootstrap:debug::'
    
    def test_canonical_ordering(self):
        """Should generate same identifier regardless of input order."""
        config1 = {
            'withouts': ['tests', 'docs', 'man'],
            'withs': ['feature', 'bootstrap']
        }
        config2 = {
            'withouts': ['man', 'docs', 'tests'],
            'withs': ['bootstrap', 'feature']
        }
        
        id1 = bcond_cache_identifier('pkg', config1)
        id2 = bcond_cache_identifier('pkg', config2)
        assert id1 == id2
        assert id1 == 'pkg:docs-man-tests:bootstrap-feature:::'
    
    def test_with_branch(self):
        """Should include non-default branch in identifier."""
        config = {'withs': ['bootstrap']}
        identifier = bcond_cache_identifier('pkg', config, branch='f38')
        assert identifier == 'pkg::bootstrap::f38:'
    
    def test_with_target(self):
        """Should include target in identifier."""
        config = {'withs': ['bootstrap']}
        identifier = bcond_cache_identifier('pkg', config, target='f38-python')
        assert identifier == 'pkg::bootstrap:::f38-python'
    
    @patch('bconds.CONFIG', {'distgit': {'branch': 'rawhide'}})
    def test_default_branch_omitted(self):
        """Should omit default branch from identifier."""
        config = {}
        identifier = bcond_cache_identifier('pkg', config, branch='rawhide')
        # Default branch should be represented as empty string
        assert identifier == 'pkg:::::'
    
    def test_single_bcond(self):
        """Should handle single bcond correctly."""
        config = {'withouts': ['tests']}
        identifier = bcond_cache_identifier('pkg', config)
        assert identifier == 'pkg:tests::::'
    
    def test_hyphen_separated(self):
        """Should use hyphens to separate multiple options."""
        config = {'withouts': ['a', 'b', 'c']}
        identifier = bcond_cache_identifier('pkg', config)
        assert 'a-b-c' in identifier


class TestKojiIdIsOlderThanWeek:
    """Tests for koji_id_is_older_than_week function."""
    
    def test_file_older_than_retention(self, tmp_path):
        """Should return True for files older than retention period."""
        koji_file = tmp_path / 'koji.id'
        koji_file.write_text('12345')
        
        eight_days_ago = datetime.datetime.now() - datetime.timedelta(days=8)
        
        with patch('os.path.getmtime', return_value=eight_days_ago.timestamp()):
            assert koji_id_is_older_than_week(koji_file)
    
    def test_file_newer_than_retention(self, tmp_path):
        """Should return False for files newer than retention period."""
        koji_file = tmp_path / 'koji.id'
        koji_file.write_text('12345')
        
        five_days_ago = datetime.datetime.now() - datetime.timedelta(days=5)
        
        with patch('os.path.getmtime', return_value=five_days_ago.timestamp()):
            assert not koji_id_is_older_than_week(koji_file)
    
    def test_file_exactly_at_retention(self, tmp_path):
        """Should return True for files exactly at retention boundary (< not <=)."""
        koji_file = tmp_path / 'koji.id'
        koji_file.write_text('12345')

        seven_days_ago = datetime.datetime.now() - datetime.timedelta(days=7)
        
        with patch('os.path.getmtime', return_value=seven_days_ago.timestamp()):
            assert koji_id_is_older_than_week(koji_file)
    
    def test_very_old_file(self, tmp_path):
        """Should return True for very old files."""
        koji_file = tmp_path / 'koji.id'
        koji_file.write_text('12345')
        
        thirty_days_ago = datetime.datetime.now() - datetime.timedelta(days=30)
        
        with patch('os.path.getmtime', return_value=thirty_days_ago.timestamp()):
            assert koji_id_is_older_than_week(koji_file)
    
    def test_fresh_file(self, tmp_path):
        """Should return False for freshly created files."""
        koji_file = tmp_path / 'koji.id'
        koji_file.write_text('12345')
        
        one_hour_ago = datetime.datetime.now() - datetime.timedelta(hours=1)
        
        with patch('os.path.getmtime', return_value=one_hour_ago.timestamp()):
            assert not koji_id_is_older_than_week(koji_file)

