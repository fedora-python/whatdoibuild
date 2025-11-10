"""Tests for utils module."""

from unittest.mock import Mock
import pytest

from utils import name_or_str, stringify


class TestNameOrStr:
    """Tests for name_or_str function."""
    
    def test_object_with_name_attribute(self):
        """Should return name attribute if present."""
        obj = Mock()
        obj.name = 'test_name'
        assert name_or_str(obj) == 'test_name'
    
    def test_object_without_name_attribute(self):
        """Should return str() representation if no name attribute."""
        # Use a simple class without name attribute
        class NoNameObj:
            def __str__(self):
                return 'string_repr'
        
        obj = NoNameObj()
        assert name_or_str(obj) == 'string_repr'
    
    def test_simple_string(self):
        """Should return string as-is."""
        assert name_or_str('hello') == 'hello'
    
    def test_number(self):
        """Should convert number to string."""
        assert name_or_str(42) == '42'
    
    def test_hawkey_package_like(self):
        """Should work with objects that have name like hawkey.Package."""
        pkg = Mock()
        pkg.name = 'python3-pytest'
        pkg.version = '7.4.0'
        assert name_or_str(pkg) == 'python3-pytest'
    
    def test_none_value(self):
        """Should convert None to string."""
        result = name_or_str(None)
        assert result == 'None'


class TestStringify:
    """Tests for stringify function."""
    
    def test_empty_list(self):
        """Should return empty string for empty list."""
        assert stringify([]) == ''
    
    def test_single_item(self):
        """Should return single item as string."""
        assert stringify(['item1']) == 'item1'
    
    def test_multiple_items_default_separator(self):
        """Should join items with comma and space by default."""
        items = ['item1', 'item2', 'item3']
        assert stringify(items) == 'item1, item2, item3'
    
    def test_custom_separator(self):
        """Should use custom separator when provided."""
        items = ['item1', 'item2', 'item3']
        assert stringify(items, '\n') == 'item1\nitem2\nitem3'
    
    def test_objects_with_name_attribute(self):
        """Should extract name from objects."""
        obj1 = Mock()
        obj1.name = 'package1'
        obj2 = Mock()
        obj2.name = 'package2'
        
        result = stringify([obj1, obj2])
        assert result == 'package1, package2'
    
    def test_mixed_objects_and_strings(self):
        """Should handle mix of objects and strings."""
        obj = Mock()
        obj.name = 'package1'
        
        result = stringify([obj, 'string_item'])
        assert result == 'package1, string_item'
    
    def test_numbers(self):
        """Should convert numbers to strings."""
        items = [1, 2, 3]
        assert stringify(items) == '1, 2, 3'
    
    def test_newline_separator(self):
        """Should work with newline separator."""
        items = ['pkg1', 'pkg2', 'pkg3']
        result = stringify(items, '\n')
        assert result == 'pkg1\npkg2\npkg3'
    
    def test_pipe_separator(self):
        """Should work with pipe separator."""
        items = ['a', 'b', 'c']
        assert stringify(items, '|') == 'a|b|c'
    
    def test_empty_separator(self):
        """Should concatenate with no separator."""
        items = ['a', 'b', 'c']
        assert stringify(items, '') == 'abc'
    
    def test_generator_input(self):
        """Should work with generator expressions."""
        gen = (x for x in ['a', 'b', 'c'])
        assert stringify(gen) == 'a, b, c'
    
    def test_tuple_input(self):
        """Should work with tuples."""
        items = ('x', 'y', 'z')
        assert stringify(items) == 'x, y, z'

