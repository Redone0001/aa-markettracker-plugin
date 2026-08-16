# Audit test suite

Run the dependency-free audit contracts with:

```bash
.venv/bin/python -m pytest -q audit_tests
```

Passing tests confirm protections already present in the source. Strict
`xfail` tests document confirmed security or quality defects. After fixing a
defect, pytest intentionally reports `XPASS(strict)` until the matching xfail
marker is removed; the test then becomes a permanent regression test.

These source-level contracts are separate from the package's Django tests
because the published package provides neither an AllianceAuth test settings
module nor working current-version integration tests.
