#!/usr/bin/env python3
"""Stand-in for the claude command. Prints a made-up note and writes nothing."""

import sys

NOTE = (
    "I am still carrying the name of the picnic checklist and have not written it down. "
    "The next session must not lose that the lemons were never bought. "
    "No promise is left open. "
    "This conversation was about a picnic that stayed a plan."
)


def main(argv):
    del argv
    sys.stdout.write(NOTE + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
