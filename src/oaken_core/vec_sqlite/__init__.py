# SPDX-License-Identifier: Apache-2.0
"""``sqlite-vec``: the vector store plugin, brute-force cosine KNN in one SQLite file.

Lifted from oaken-mail ``oaken_vec_sqlite`` (cf3b187) and cut down to core's contract: vectors are
filed under a collection and a key, with no message, account or model bookkeeping. Registered in the
``oaken_core.vector_stores`` entry-point group.
"""

from oaken_core.vec_sqlite.store import SqliteVecStore

__all__ = ["SqliteVecStore"]
