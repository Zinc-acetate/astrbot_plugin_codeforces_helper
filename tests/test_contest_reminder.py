import unittest

from core.contest_reminder import (
    build_reminder_specs,
    classify_contest,
    contest_is_enabled,
    format_reminder_offset,
    parse_group_whitelist,
    parse_reminder_offsets,
)


class ContestReminderParsingTests(unittest.TestCase):
    def test_plain_values_default_to_hours_and_units_are_supported(self):
        self.assertEqual(
            parse_reminder_offsets("24 1h 30min 10s 1d"),
            (86400, 3600, 1800, 10),
        )

    def test_empty_times_disable_reminders(self):
        self.assertEqual(parse_reminder_offsets("   "), ())

    def test_invalid_or_unsafe_times_are_rejected(self):
        for value in ("0", "-1h", "1m", "1.5h", "366d"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_reminder_offsets(value)

    def test_group_whitelist_accepts_list_or_separated_text(self):
        groups, invalid = parse_group_whitelist(["123", "456 789", "123", "bad"])
        self.assertEqual(groups, (123, 456, 789))
        self.assertEqual(invalid, ("bad",))

        groups, invalid = parse_group_whitelist("123, 456\n789")
        self.assertEqual(groups, (123, 456, 789))
        self.assertEqual(invalid, ())


class ContestReminderClassificationTests(unittest.TestCase):
    def test_contest_categories(self):
        cases = {
            "Educational Codeforces Round 190 (Rated for Div. 2)": "educational",
            "Codeforces Round 1100 (Div. 1 + Div. 2)": "div2",
            "Codeforces Round 1101 (Div. 3)": "div3",
            "Codeforces Round 1102 (Div. 4)": "div4",
            "Codeforces Global Round 40": "other",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(classify_contest(name), expected)

    def test_filter_switches_are_independent(self):
        config = {
            "include_div2": False,
            "include_div3": True,
            "include_div4": False,
            "include_educational": False,
            "include_other": False,
        }
        self.assertTrue(contest_is_enabled("Codeforces Round 1101 (Div. 3)", config))
        self.assertFalse(contest_is_enabled("Codeforces Round 1100 (Div. 2)", config))
        self.assertFalse(
            contest_is_enabled(
                "Educational Codeforces Round 190 (Rated for Div. 2)",
                config,
            )
        )


class ContestReminderScheduleTests(unittest.TestCase):
    def test_only_future_enabled_reminders_are_built(self):
        now = 1_000_000
        contests = [
            {
                "id": 100,
                "name": "Codeforces Round 1100 (Div. 2)",
                "phase": "BEFORE",
                "startTimeSeconds": now + 7200,
            },
            {
                "id": 101,
                "name": "Educational Codeforces Round 190",
                "phase": "BEFORE",
                "startTimeSeconds": now + 7200,
            },
            {
                "id": 102,
                "name": "Codeforces Round 1099 (Div. 2)",
                "phase": "FINISHED",
                "startTimeSeconds": now + 7200,
            },
        ]
        config = {
            "include_div2": True,
            "include_div3": False,
            "include_div4": False,
            "include_educational": False,
            "include_other": False,
        }

        specs = build_reminder_specs(
            contests,
            offsets=(10800, 3600),
            filter_config=config,
            now_timestamp=now,
        )

        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].job_id, "cf_contest_reminder_100_3600")
        self.assertEqual(specs[0].run_at_timestamp, now + 3600)
        self.assertEqual(specs[0].category, "div2")

    def test_offset_labels(self):
        self.assertEqual(format_reminder_offset(86400), "1天")
        self.assertEqual(format_reminder_offset(7200), "2小时")
        self.assertEqual(format_reminder_offset(120), "2分钟")
        self.assertEqual(format_reminder_offset(10), "10秒")


if __name__ == "__main__":
    unittest.main()
