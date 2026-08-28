.PHONY: test verify

test:
	PYTHONPATH=. python3 -m unittest discover -s tests -v

verify: test
	PYTHONPATH=. python3 -m py_compile fleet_control/*.py scripts/*.py
