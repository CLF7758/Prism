"""Independent acceptance for the model library.

This package holds the *expectations*, not the implementation. Nothing here
calls the Prism importer, the F3D backend or the exporter: a test that used
the code under test to produce its own expectation would only prove that the
code agrees with itself.
"""
