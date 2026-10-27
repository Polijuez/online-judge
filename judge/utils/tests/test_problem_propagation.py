import time

from django.test import SimpleTestCase, override_settings

from judge.utils.problem_propagation import propagate_problem_change
from judge.utils.tests.fake_judge import FakeJudgeControlServer, closed_port_endpoint

PROBLEM_ID = '00000000-0000-0000-0000-000000000000'


class PropagateProblemChangeTestCase(SimpleTestCase):
    def fake_judge(self, **kwargs):
        judge = FakeJudgeControlServer(**kwargs)
        self.addCleanup(judge.close)
        return judge

    @override_settings(DMOJ_JUDGE_CONTROL_TIMEOUT=1.0)
    def test_every_configured_judge_is_asked_to_rescan(self):
        judges = [self.fake_judge(), self.fake_judge()]

        with override_settings(DMOJ_JUDGE_CONTROL_ENDPOINTS=[judge.url for judge in judges]):
            results = propagate_problem_change(PROBLEM_ID)

        self.assertEqual([result.ok for result in results], [True, True])
        self.assertEqual([result.status for result in results], [200, 200])
        self.assertEqual([judge.requests for judge in judges], [['/update/problems'], ['/update/problems']])

    @override_settings(DMOJ_JUDGE_CONTROL_ENDPOINTS=[])
    def test_without_configured_judges_nothing_is_sent(self):
        self.assertEqual(propagate_problem_change(PROBLEM_ID), ())

    @override_settings(DMOJ_JUDGE_CONTROL_TIMEOUT=0.5)
    def test_a_down_judge_is_logged_and_never_raises(self):
        endpoint = closed_port_endpoint()

        with override_settings(DMOJ_JUDGE_CONTROL_ENDPOINTS=[endpoint]):
            with self.assertLogs('judge.problem_propagation', level='WARNING') as logs:
                results = propagate_problem_change(PROBLEM_ID)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertIsNone(results[0].status)
        self.assertIn(endpoint, '\n'.join(logs.output))

    @override_settings(DMOJ_JUDGE_CONTROL_TIMEOUT=0.6)
    def test_a_hanging_judge_is_cut_off_by_the_timeout_and_judges_run_in_parallel(self):
        judges = [self.fake_judge(delay=5) for _ in range(3)]
        endpoints = [judge.url for judge in judges]

        with override_settings(DMOJ_JUDGE_CONTROL_ENDPOINTS=endpoints):
            with self.assertLogs('judge.problem_propagation', level='WARNING') as logs:
                started = time.monotonic()
                results = propagate_problem_change(PROBLEM_ID)
                elapsed = time.monotonic() - started

        # En serie serían tres timeouts (1.8s); los avisos salen en paralelo.
        self.assertLess(elapsed, 1.2)
        self.assertTrue(all(not result.ok for result in results))
        self.assertTrue(all(result.error for result in results))
        logged = '\n'.join(logs.output)
        for endpoint in endpoints:
            self.assertIn(endpoint, logged)
