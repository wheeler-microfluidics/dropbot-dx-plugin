from datetime import datetime
import logging
import sys

from path_helpers import path
from pip_helpers import install


def main(ostream=sys.stdout):
    logging.basicConfig(level=logging.INFO)
    plugin_root = path(__file__).parent.abspath()
    logging.info('[%s] Processing post-install hook for: %s',
                 str(datetime.now()), plugin_root.name)
    requirements_file = plugin_root.joinpath('requirements.txt')
    if requirements_file.exists():
        # Install required packages using `pip`, with Wheeler Lab wheels server
        # for binary wheels not available on `PyPi`.
        print >> ostream, install(['--find-links', 'http://192.99.4.95/wheels',
                                   '--trusted-host', '192.99.4.95',
                                   '--no-cache', '-r', requirements_file])
        print >> ostream, ('[%s] Completed post-install processing for: %s' %
                           (str(datetime.now()), plugin_root.name))


if __name__ == '__main__':
    main()
