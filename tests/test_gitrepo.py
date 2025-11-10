"""Tests for gitrepo module."""

import pytest

from gitrepo import validate_bcond_config, generate_bcond_lines, apply_macro_replacements


class TestValidateBcondConfig:
    """Tests for validate_bcond_config function."""
    
    def test_no_conflict(self):
        """Should not raise when withs and withouts don't overlap."""
        config = {'withs': ['bootstrap'], 'withouts': ['tests']}
        validate_bcond_config(config)
    
    def test_empty_config(self):
        """Should not raise for empty configuration."""
        config = {}
        validate_bcond_config(config)
    
    def test_only_withs(self):
        """Should not raise when only withs are specified."""
        config = {'withs': ['bootstrap', 'feature']}
        validate_bcond_config(config)
    
    def test_only_withouts(self):
        """Should not raise when only withouts are specified."""
        config = {'withouts': ['tests', 'docs']}
        validate_bcond_config(config)
    
    def test_single_conflict(self):
        """Should raise ValueError for single overlapping bcond."""
        config = {'withs': ['bootstrap'], 'withouts': ['bootstrap']}
        with pytest.raises(ValueError, match='Cannot have the same with and without: bootstrap'):
            validate_bcond_config(config)
    
    def test_multiple_conflicts(self):
        """Should raise ValueError listing all overlapping bconds."""
        config = {
            'withs': ['bootstrap', 'tests', 'feature'],
            'withouts': ['tests', 'docs', 'feature']
        }
        with pytest.raises(ValueError) as exc_info:
            validate_bcond_config(config)
        
        error_msg = str(exc_info.value)
        assert 'feature' in error_msg
        assert 'tests' in error_msg
        assert 'bootstrap' not in error_msg  # bootstrap is not in conflict
        assert 'docs' not in error_msg  # docs is not in conflict


class TestGenerateBcondLines:
    """Tests for generate_bcond_lines function."""
    
    def test_empty_config(self):
        """Should return empty list for empty configuration."""
        config = {}
        assert generate_bcond_lines(config) == []
    
    def test_only_withouts(self):
        """Should generate only _without_* macros."""
        config = {'withouts': ['tests', 'docs']}
        lines = generate_bcond_lines(config)
        assert lines == [
            '%global _without_docs 1',
            '%global _without_tests 1'
        ]
    
    def test_only_withs(self):
        """Should generate only _with_* macros."""
        config = {'withs': ['bootstrap', 'feature']}
        lines = generate_bcond_lines(config)
        assert lines == [
            '%global _with_bootstrap 1',
            '%global _with_feature 1'
        ]
    
    def test_both_withs_and_withouts(self):
        """Should generate both types of macros in correct order."""
        config = {
            'withouts': ['tests', 'docs'],
            'withs': ['bootstrap']
        }
        lines = generate_bcond_lines(config)
        assert lines == [
            '%global _without_docs 1',
            '%global _without_tests 1',
            '%global _with_bootstrap 1'
        ]
    
    def test_sorted_output(self):
        """Should sort bconds alphabetically."""
        config = {
            'withouts': ['zed', 'alpha', 'middle'],
            'withs': ['zebra', 'aardvark']
        }
        lines = generate_bcond_lines(config)
        assert lines == [
            '%global _without_alpha 1',
            '%global _without_middle 1',
            '%global _without_zed 1',
            '%global _with_aardvark 1',
            '%global _with_zebra 1'
        ]
    
    def test_single_bcond(self):
        """Should handle single bcond correctly."""
        config = {'withouts': ['tests']}
        lines = generate_bcond_lines(config)
        assert lines == ['%global _without_tests 1']


class TestApplyMacroReplacements:
    """Tests for apply_macro_replacements function."""
    
    def test_no_replacements(self):
        """Should return unchanged text when no replacements."""
        spec_text = '%global version 1.0\n%define release 1'
        result = apply_macro_replacements(spec_text, {})
        assert result == spec_text
    
    def test_simple_global_replacement(self):
        """Should replace %global macro value."""
        spec_text = '%global version 1.0'
        replacements = {'version': '2.0'}
        result = apply_macro_replacements(spec_text, replacements)
        assert result == '%global version 2.0'
    
    def test_simple_define_replacement(self):
        """Should replace %define macro value."""
        spec_text = '%define release 1'
        replacements = {'release': '2'}
        result = apply_macro_replacements(spec_text, replacements)
        assert result == '%define release 2'
    
    def test_multiple_replacements(self):
        """Should handle multiple macro replacements."""
        spec_text = '''%global version 1.0
%define release 1
%global someflag 0'''
        replacements = {'version': '2.0', 'someflag': '1'}
        result = apply_macro_replacements(spec_text, replacements)
        expected = '''%global version 2.0
%define release 1
%global someflag 1'''
        assert result == expected
    
    def test_macro_with_special_chars(self):
        """Should handle macro names with special regex characters."""
        spec_text = '%global some.macro 0'
        replacements = {'some.macro': '1'}
        result = apply_macro_replacements(spec_text, replacements)
        assert result == '%global some.macro 1'
    
    def test_macro_with_underscores(self):
        """Should handle macro names with underscores."""
        spec_text = '%global _with_feature 0'
        replacements = {'_with_feature': '1'}
        result = apply_macro_replacements(spec_text, replacements)
        assert result == '%global _with_feature 1'
    
    def test_preserves_indentation(self):
        """Should preserve leading whitespace."""
        spec_text = '  %global version 1.0\n\t%define release 1'
        replacements = {'version': '2.0'}
        result = apply_macro_replacements(spec_text, replacements)
        assert result == '  %global version 2.0\n\t%define release 1'
    
    def test_preserves_spacing(self):
        """Should preserve spacing between elements."""
        spec_text = '%global  version   1.0'
        replacements = {'version': '2.0'}
        result = apply_macro_replacements(spec_text, replacements)
        # The regex should preserve the spacing structure
        assert '%global' in result
        assert 'version' in result
        assert '2.0' in result
    
    def test_only_replaces_matching_macro(self):
        """Should only replace the specified macro, not similar ones."""
        spec_text = '''%global version 1.0
%global version_major 1
%global my_version 1.0'''
        replacements = {'version': '2.0'}
        result = apply_macro_replacements(spec_text, replacements)
        expected = '''%global version 2.0
%global version_major 1
%global my_version 1.0'''
        assert result == expected
    
    def test_multiline_spec(self):
        """Should work correctly with multiline spec files."""
        spec_text = '''Name: mypackage
Version: 1.0
%global debug_package %{nil}
%global with_tests 0

Summary: A test package'''
        replacements = {'with_tests': '1'}
        result = apply_macro_replacements(spec_text, replacements)
        assert '%global with_tests 1' in result
        assert 'Name: mypackage' in result
        assert 'Summary: A test package' in result

