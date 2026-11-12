import loadgov


def test_package_imports_with_version():
    assert loadgov.__version__ == "0.0.1"
