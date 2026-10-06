"""Ingestion-level tests: what the streaming parser lands in SQLite.

Every expectation is hand-counted from the fixture history (see
``tests/fixture.py``), so a parser regression (rename attribution, binary
handling, mailmap rewriting, merge exclusion) fails loudly here.
"""
import unittest

from tests import fixture as fx


class IngestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = fx.shared()
        cls.conn = cls.fx.conn
        cls.rid = cls.fx.repo_id

    def scalar(self, sql: str, params=()):
        return self.conn.execute(sql, params).fetchone()[0]

    def test_repo_row_statistics(self):
        row = self.conn.execute(
            "SELECT commits_count, authors_count, files_count, ref_hash, ref"
            " FROM repos WHERE id = ?", (self.rid,),
        ).fetchone()
        self.assertEqual(
            tuple(row[:3]),
            (fx.EXPECTED["commits"], fx.EXPECTED["authors"], fx.EXPECTED["files"]),
        )
        self.assertEqual(len(row[3]), 40)
        self.assertEqual(row[4], "HEAD")

    def test_analysed_ref_is_head(self):
        head = self.conn.execute(
            "SELECT hash FROM commits WHERE repo_id = ? ORDER BY cdate DESC LIMIT 1",
            (self.rid,),
        ).fetchone()[0]
        self.assertEqual(len(head), 40)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM commits WHERE repo_id = ?", (self.rid,)), 6)

    def test_merge_commit_excluded(self):
        # 7 commits exist in the repository; the --no-ff merge must be skipped.
        names = {
            row[0] for row in self.conn.execute(
                "SELECT name FROM authors WHERE repo_id = ?", (self.rid,)
            )
        }
        self.assertNotIn("Dave Merger", names)  # author of the merge only

    def test_mailmap_folds_alias_identity(self):
        names = {
            row[0] for row in self.conn.execute(
                "SELECT name FROM authors WHERE repo_id = ? ORDER BY name", (self.rid,)
            )
        }
        self.assertEqual(names, {"Alice Example", "Bob Builder", "Carol Coder"})

    def test_rename_attributed_to_new_path(self):
        row = self.conn.execute(
            "SELECT added, deleted, old_path FROM changes"
            " WHERE repo_id = ? AND path = 'src/c.py'", (self.rid,),
        ).fetchone()
        self.assertIsNotNone(row, "rename target src/c.py missing from changes")
        self.assertEqual((row[0], row[1], row[2]), (1, 0, "src/a.py"))  # +1 line on the new path
        # The old path keeps its own pre-rename history.
        added = self.scalar(
            "SELECT COALESCE(SUM(added),0) FROM changes"
            " WHERE repo_id = ? AND path = 'src/a.py' AND old_path IS NULL", (self.rid,),
        )
        self.assertEqual(added, 6)  # 4 (c1) + 2 (c2)

    def test_binary_recorded_but_not_measured(self):
        row = self.conn.execute(
            "SELECT added, deleted, is_binary FROM changes"
            " WHERE repo_id = ? AND path = 'assets/logo.png'", (self.rid,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual((row[0], row[1], row[2]), (0, 0, 1))
        self.assertEqual(
            self.scalar("SELECT is_binary FROM files WHERE repo_id = ? AND path = 'assets/logo.png'", (self.rid,)),
            1,
        )

    def test_hand_counted_totals(self):
        added, removed = self.conn.execute(
            "SELECT SUM(added), SUM(deleted) FROM dir_changes"
            " WHERE repo_id = ? AND path = ''", (self.rid,),
        ).fetchone()
        self.assertEqual((int(added), int(removed)), (fx.EXPECTED["added"], fx.EXPECTED["removed"]))
        mods = self.scalar(
            "SELECT COUNT(DISTINCT commit_id) FROM dir_changes"
            " WHERE repo_id = ? AND path = '' AND added + deleted > 0", (self.rid,),
        )
        self.assertEqual(mods, fx.EXPECTED["commits"])  # every commit changes at least one line

    def test_file_universe_includes_history_and_head(self):
        paths = {
            row[0] for row in self.conn.execute(
                "SELECT path FROM files WHERE repo_id = ?", (self.rid,)
            )
        }
        self.assertEqual(len(paths), fx.EXPECTED["files"])
        self.assertIn("src/a.py", paths)       # not in HEAD, but in history
        self.assertIn("feature.txt", paths)    # only reachable via the merged branch
        self.assertIn("assets/logo.png", paths)


if __name__ == "__main__":
    unittest.main()
