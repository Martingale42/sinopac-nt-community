# Changelog

## [0.1.1]
- First release published to PyPI (`pip install sinopac-nt-community`) and crates.io
  (`cargo add sinopac-nt-community`). No functional changes from 0.1.0.

## [0.1.0]
- Initial standalone extraction from the NautilusTrader fork (sinopac-adapter-clean
  @ 034c70e788): live data + execution clients and instrument provider for SinoPac
  (Taiwan markets) via Shioaji.
- Emulated stop / conditional order handling (rejected with an OrderEmulator hint).
- Rust core re-exported for pure-Rust use. Pinned to nautilus_trader 1.228.0 /
  nautilus-* 0.58.0.
