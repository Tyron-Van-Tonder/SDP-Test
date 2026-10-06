"""Metric-engine tests: cross-family invariants over the fixture history.

These assert the brief's semantics end to end — the root rollup equals the
repository totals, the top-level partition and the author/series partitions
close exactly, commit-set filters (range/list, full/abbreviated hashes)
agree with the underlying commit count, and manual author merges fold and
restore without changing any measurement.
"""
import unittest

import db
import metrics
from metrics import CommitFilter
from tests import fixture as fx


class MetricInvariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = fx.shared()
        cls.conn = cls.fx.conn
        cls.rid = cls.fx.repo_id
        cls.canonical = db.author_canonical(cls.conn, cls.rid)

    def canonical_map(self):
        return db.author_canonical(self.conn, self.rid)

    # -- repository totals ---------------------------------------------------

    def test_totals_match_hand_count(self):
        t = metrics.totals(self.conn, self.rid, CommitFilter())
        self.assertEqual(
            (t["added"], t["removed"], t["churn"], t["growth"], t["commits"]),
            (fx.EXPECTED["added"], fx.EXPECTED["removed"], fx.EXPECTED["churn"],
             fx.EXPECTED["growth"], fx.EXPECTED["commits"]),
        )
        self.assertAlmostEqual(t["churn_rate"], fx.EXPECTED["churn"] / fx.EXPECTED["commits"])

    def test_root_directory_equals_totals(self):
        totals = metrics.totals(self.conn, self.rid, CommitFilter())
        root = next(
            d for d in metrics.dir_metrics(self.conn, self.rid, CommitFilter())
            if d["path"] == ""
        )
        for key in ("added", "removed", "churn", "modifications"):
            self.assertEqual(root[key], totals[key], key)

    def test_top_level_partition(self):
        totals = metrics.totals(self.conn, self.rid, CommitFilter())
        files = metrics.file_metrics(self.conn, self.rid, CommitFilter())
        dirs = metrics.dir_metrics(self.conn, self.rid, CommitFilter())
        root_files = [f for f in files if "/" not in f["path"]]
        top_dirs = [d for d in dirs if d["path"] and "/" not in d["path"]]
        self.assertEqual(sum(f["added"] for f in root_files), fx.EXPECTED["root_files_added"])
        self.assertEqual(sum(d["added"] for d in top_dirs) + fx.EXPECTED["root_files_added"],
                         totals["added"])
        self.assertEqual(sum(d["removed"] for d in top_dirs)
                         + sum(f["removed"] for f in root_files), totals["removed"])

    def test_deep_directory_equals_its_files(self):
        dirs = {d["path"]: d for d in metrics.dir_metrics(self.conn, self.rid, CommitFilter())}
        self.assertEqual(dirs["src"]["added"], fx.EXPECTED["src_added"])
        self.assertEqual(dirs["src/sub"]["added"], fx.EXPECTED["src_sub_added"])
        files = metrics.file_metrics(self.conn, self.rid, CommitFilter())
        under_sub = [f for f in files if f["path"].startswith("src/sub/")]
        self.assertEqual(sum(f["added"] for f in under_sub), dirs["src/sub"]["added"])
        self.assertEqual(sum(f["removed"] for f in under_sub), dirs["src/sub"]["removed"])

    def test_untouched_binary_listed_without_lines(self):
        files = metrics.file_metrics(self.conn, self.rid, CommitFilter(), include_untouched=True)
        logo = next(f for f in files if f["path"] == "assets/logo.png")
        self.assertEqual((logo["is_binary"], logo["churn"], logo["modifications"]), (1, 0, 0))

    # -- authors -------------------------------------------------------------

    def test_author_partition_and_rows(self):
        totals = metrics.totals(self.conn, self.rid, CommitFilter())
        rows = metrics.authors_overview(self.conn, self.rid, self.canonical_map())
        by_name = {r["name"]: r for r in rows}
        self.assertEqual(set(by_name), {"Alice Example", "Bob Builder", "Carol Coder"})

        alice, bob, carol = by_name["Alice Example"], by_name["Bob Builder"], by_name["Carol Coder"]
        self.assertEqual((alice["added"], alice["removed"], alice["commits"]), (10, 0, 2))
        self.assertEqual((bob["added"], bob["removed"], bob["commits"]), (6, 1, 3))
        self.assertEqual((carol["added"], carol["removed"], carol["commits"]), (5, 0, 1))

        self.assertEqual(sum(r["churn"] for r in rows), totals["churn"])
        self.assertEqual(sum(r["commits"] for r in rows), totals["commits"])

    def test_author_filter_equals_author_row(self):
        rows = metrics.authors_overview(self.conn, self.rid, self.canonical_map())
        alice = next(r for r in rows if r["name"] == "Alice Example")
        members = metrics.resolve_author_members(self.canonical_map(), [alice["id"]])
        t = metrics.totals(self.conn, self.rid, CommitFilter(), members)
        self.assertEqual((t["added"], t["removed"], t["commits"]),
                         (alice["added"], alice["removed"], alice["commits"]))

    def test_merge_and_unmerge_authors(self):
        bob = self.fx.author_id("Bob Builder")
        carol = self.fx.author_id("Carol Coder")
        before = metrics.totals(self.conn, self.rid, CommitFilter())
        try:
            canonical = metrics.merge_authors(self.conn, self.rid, carol, bob)
            rows = metrics.authors_overview(self.conn, self.rid, canonical)
            self.assertEqual(len(rows), 2)
            merged = next(r for r in rows if r["id"] == canonical[bob])
            self.assertTrue(merged["merged"])
            self.assertEqual((merged["churn"], merged["commits"]), (12, 4))  # Bob 7+5 / 3+1

            # A filter naming either member resolves to the merged group.
            members = metrics.resolve_author_members(canonical, [carol])
            self.assertEqual(members, {bob, carol})
            t = metrics.totals(self.conn, self.rid, CommitFilter(), members)
            self.assertEqual((t["churn"], t["commits"]), (12, 4))

            # Merging is presentation-only: totals never move.
            self.assertEqual(metrics.totals(self.conn, self.rid, CommitFilter()), before)
            with self.assertRaises(ValueError):
                metrics.merge_authors(self.conn, self.rid, bob, carol)
        finally:
            canonical = metrics.unmerge_author(self.conn, self.rid, carol)
        rows = metrics.authors_overview(self.conn, self.rid, canonical)
        self.assertEqual(len(rows), 3)
        self.assertEqual(metrics.totals(self.conn, self.rid, CommitFilter()), before)

    # -- time series ---------------------------------------------------------

    def test_series_partition(self):
        totals = metrics.totals(self.conn, self.rid, CommitFilter())
        for bucket, expected_points in (("day", 6), ("month", 1)):
            points = metrics.series(self.conn, self.rid, CommitFilter(), bucket=bucket)
            self.assertEqual(len(points), expected_points, bucket)
            self.assertEqual(sum(p["added"] for p in points), totals["added"], bucket)
            self.assertEqual(sum(p["removed"] for p in points), totals["removed"], bucket)
            self.assertEqual(sum(p["commits"] for p in points), totals["commits"], bucket)
        month = metrics.series(self.conn, self.rid, CommitFilter(), bucket="month")[0]
        self.assertEqual(month["bucket"], "2024-01")
        self.assertEqual((month["commits"], month["modifications"]), (6, 6))

    def test_object_series_is_subset_of_repo_series(self):
        repo_day = {
            p["bucket"]: p
            for p in metrics.series(self.conn, self.rid, CommitFilter(), bucket="day")
        }
        obj_day = metrics.object_series(self.conn, self.rid, CommitFilter(), "dir", "src", bucket="day")
        self.assertTrue(obj_day)
        for point in obj_day:
            self.assertLessEqual(point["churn"], repo_day[point["bucket"]]["churn"])

    # -- commit-set filters --------------------------------------------------

    def test_range_filter_is_half_open(self):
        # [2024-01-01, 2024-01-03) covers c1 and c2 only (c3 starts exactly at the end).
        flt = metrics.filter_from_payload({
            "mode": "range", "start": fx.BASE_EPOCH, "end": fx.BASE_EPOCH + 2 * fx.DAY,
        })
        t = metrics.totals(self.conn, self.rid, flt)
        self.assertEqual((t["commits"], t["added"], t["removed"]), (2, 11, 1))
        boundary = metrics.filter_from_payload({
            "mode": "range",
            "start": fx.BASE_EPOCH + 2 * fx.DAY,
            "end": fx.BASE_EPOCH + 2 * fx.DAY + 1,
        })
        self.assertEqual(metrics.commit_count(self.conn, self.rid, boundary), 1)

    def test_hash_filter_full_and_abbreviated(self):
        full = self.fx.newest_hash  # c6 (2024-01-06): +1 line, alone
        for spelling in (full, full[:8].upper(), full[:4]):
            flt = metrics.filter_from_payload({"mode": "list", "hashes": [spelling]})
            self.assertEqual(metrics.commit_count(self.conn, self.rid, flt), 1, spelling)
            t = metrics.totals(self.conn, self.rid, flt)
            self.assertEqual((t["added"], t["removed"]), (1, 0), spelling)
        # Duplicates collapse to the same commit set.
        flt = metrics.filter_from_payload({"mode": "list", "hashes": [full, full[:8]]})
        self.assertEqual(metrics.commit_count(self.conn, self.rid, flt), 1)

    def test_unknown_hashes_are_reported(self):
        unknown = metrics.unknown_hashes(
            self.conn, self.rid, [self.fx.newest_hash, self.fx.newest_hash[:8], "deadbeef", "zz!!"]
        )
        self.assertEqual(unknown, ["deadbeef", "zz!!"])
        flt = metrics.filter_from_payload({"mode": "list", "hashes": ["deadbeef", "zz!!"]})
        t = metrics.totals(self.conn, self.rid, flt)
        self.assertEqual((t["commits"], t["churn"], t["mod_freq"], t["churn_rate"]), (0, 0, 0.0, 0.0))

    # -- object detail -------------------------------------------------------

    def test_object_detail_ownership_partition(self):
        for kind, path, expected_added, expected_mods in (
            ("dir", "src", fx.EXPECTED["src_added"], 5),
        ):
            detail = metrics.object_detail(
                self.conn, self.rid, CommitFilter(), kind, path, self.canonical_map()
            )
            self.assertEqual(detail["totals"]["added"], expected_added)
            self.assertEqual(detail["totals"]["modifications"], expected_mods)
            self.assertAlmostEqual(sum(a["ownership"] for a in detail["authors"]), 1.0)
            self.assertEqual(len(detail["authors"]), 3)  # Alice, Bob, Carol each own 5/15

    def test_rename_target_owner_is_canonical_identity(self):
        detail = metrics.object_detail(
            self.conn, self.rid, CommitFilter(), "file", "src/c.py", self.canonical_map()
        )
        alice = self.fx.author_id("Alice Example")
        self.assertEqual(detail["totals"]["added"], 1)
        self.assertEqual([a["id"] for a in detail["authors"]], [alice])
        self.assertAlmostEqual(detail["authors"][0]["ownership"], 1.0)


if __name__ == "__main__":
    unittest.main()
