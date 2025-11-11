"""Tests for build module."""

import pathlib
from unittest.mock import patch

from build import parse_component_argument, get_patch_path, get_spec_path


class TestParseComponentArgument:
    """Tests for parse_component_argument function."""
    
    def test_simple_component_name(self):
        """Should parse simple component name without bcond."""
        component, bootstrap = parse_component_argument('python-pytest')
        assert component == 'python-pytest'
        assert bootstrap is None
    
    @patch('build.build_reverse_id_lookup')
    @patch('build.reverse_id_lookup', {'pkg:tests:::': {'withouts': ['tests']}})
    def test_bcond_identifier(self, mock_build):
        """Should parse bcond identifier and lookup config."""
        component, bootstrap = parse_component_argument('pkg:tests:::')
        assert component == 'pkg'
        assert bootstrap == {'withouts': ['tests']}
        mock_build.assert_called_once()
    
    @patch('build.build_reverse_id_lookup')
    @patch('build.reverse_id_lookup', {'complex:docs-tests:bootstrap::': {'withouts': ['docs', 'tests'], 'withs': ['bootstrap']}})
    def test_complex_bcond_identifier(self, mock_build):
        """Should parse complex bcond identifier."""
        component, bootstrap = parse_component_argument('complex:docs-tests:bootstrap::')
        assert component == 'complex'
        assert bootstrap == {'withouts': ['docs', 'tests'], 'withs': ['bootstrap']}
    
    def test_component_with_hyphen(self):
        """Should handle component names with hyphens."""
        component, bootstrap = parse_component_argument('python-some-package')
        assert component == 'python-some-package'
        assert bootstrap is None
    
    def test_component_with_number(self):
        """Should handle component names with numbers."""
        component, bootstrap = parse_component_argument('python3')
        assert component == 'python3'
        assert bootstrap is None


class TestGetPatchPath:
    """Tests for get_patch_path function."""
    
    def test_simple_component(self):
        """Should generate correct patch path."""
        path = get_patch_path('mypackage')
        assert isinstance(path, pathlib.Path)
        assert path.name == 'mypackage.patch'
        assert 'patches_dir' in str(path)
    
    def test_component_with_hyphen(self):
        """Should handle hyphens in component name."""
        path = get_patch_path('python-pytest')
        assert path.name == 'python-pytest.patch'
        assert path.parent.name == 'patches_dir'


class TestGetSpecPath:
    """Tests for get_spec_path function."""
    
    def test_simple_spec_path(self):
        """Should generate correct spec path."""
        repopath = pathlib.Path('/tmp/repos/mypackage')
        specpath = get_spec_path(repopath, 'mypackage')
        
        assert isinstance(specpath, pathlib.Path)
        assert specpath.name == 'mypackage.spec'
        assert specpath.parent == repopath
    
    def test_spec_path_with_hyphen(self):
        """Should handle hyphens in component name."""
        repopath = pathlib.Path('/tmp/repos/python-pytest')
        specpath = get_spec_path(repopath, 'python-pytest')
        
        assert specpath.name == 'python-pytest.spec'
    
    def test_spec_path_relative(self):
        """Should work with relative paths."""
        repopath = pathlib.Path('repos/pkg')
        specpath = get_spec_path(repopath, 'pkg')
        
        assert specpath == pathlib.Path('repos/pkg/pkg.spec')

