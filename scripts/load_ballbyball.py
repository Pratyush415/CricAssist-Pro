"""One-off script: build the local stats database from files in data/raw/.

Same as `cricassist build-db`; accepts the same options.
"""

import sys

from cricassist.main import main

if __name__ == "__main__":
    sys.exit(main(["build-db", *sys.argv[1:]]))
