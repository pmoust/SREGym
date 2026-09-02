"""Point the editable install at the checkout the tests live in.

`pip install -e .` writes a finder onto sys.meta_path whose MAPPING hardcodes
absolute paths to the checkout it was installed from:

    MAPPING = {'clients': '/Users/.../sregym/clients', 'sregym': '/Users/.../sregym/sregym', ...}

sys.meta_path is consulted before sys.path, so in a linked worktree every
`import sregym...` and `import clients...` resolves to the *main* checkout no
matter where pytest runs. A branch's tests then exercise main's code and pass,
which is worse than failing: the suite reports green on code the branch does
not contain.

That is not a corner case here. One ticket, one worktree, one branch is how
this repo works, so every run outside the primary checkout hits it.

So retarget the mapping at whichever tree this conftest sits in. Only entries
that exist here are rewritten, so a package the worktree does not carry still
resolves to the installed copy.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _retarget_editable_install() -> None:
    for name, module in list(sys.modules.items()):
        if not (name.startswith("__editable___") and name.endswith("_finder")):
            continue

        mapping = getattr(module, "MAPPING", None)
        if not isinstance(mapping, dict):
            continue

        for package, installed in list(mapping.items()):
            local = ROOT / package
            if local.is_dir() and Path(installed) != local:
                mapping[package] = str(local)


_retarget_editable_install()
