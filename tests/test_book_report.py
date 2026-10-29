import unittest

from linemaker.book_report import book_lines, book_scores, suggested_weights


def rec(winner, books, bet_type="prop"):
    return {"inputs": {"bet_type": bet_type}, "result": {"winner": winner},
            "history": [{"t": "x", "A": {b: a for b, (a, _o) in books.items()},
                         "B": {b: o for b, (_a, o) in books.items()}}]}


class BookReportTest(unittest.TestCase):
    def test_the_book_that_called_it_ranks_first(self):
        records = []
        for i in range(200):   # Sharp prices the over right (it hits); Soft has it a coin flip
            records.append(rec("A", {"Sharp": (-200, 165), "Soft": (-110, -110), "Mid": (-140, 115)}))
        s = book_scores(records)["props"]
        self.assertLess(s["Sharp"]["brier"], s["Mid"]["brier"])
        self.assertLess(s["Mid"]["brier"], s["Soft"]["brier"])
        w = suggested_weights(s)
        self.assertEqual(w["Sharp"], 100)
        self.assertEqual(w["Soft"], 20)
        text = "\n".join(book_lines(records))
        self.assertIn("Suggested: `Sharp:100", text)
        self.assertNotIn("Game lines", text)

    def test_one_book_markets_skipped(self):
        self.assertEqual(book_scores([rec("A", {"Only": (-110, -110)})])["props"], {})
