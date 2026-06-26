def test_extension_imports():
    from sinopac_nt import _sinopac

    assert _sinopac.SINOPAC == "SINOPAC"
    for name in ("SinopacAction", "SinopacHttpClient", "SinopacWebSocketClient"):
        assert hasattr(_sinopac, name)


def test_package_imports():
    import sinopac_nt.factories  # exercises the rewired imports + framework imports
