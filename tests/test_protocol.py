import unittest
import tempfile
from types import SimpleNamespace
import sys
from pathlib import Path
from bench_tools.swebench_runner.feedback import normalize,rejected_paths
from bench_tools.jiuwenswarm_runtime.ledger import Ledger


class ProtocolTests(unittest.TestCase):
    def test_paired_seed_is_not_charged_to_repair_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=Ledger(directory,max_submissions=2,repeated_limit=5,submission_offset=1)
            value={'status':'product_failure','failure_id':'same','resolved':False}
            first=ledger.record('repair1','key1','sha1',value)['feedback']
            self.assertEqual((first['submission_id'],first['remaining_submissions'],first['same_failure_count']),(2,1,1))
            self.assertNotIn('terminal',first)
            second=ledger.record('repair2','key2','sha2',value)['feedback']
            self.assertEqual((second['submission_id'],second['terminal']),(3,'submission_budget'))

    def test_process_exit_zero_is_not_success(self):
        value=normalize(None,'',case_id='x',exit_code=0)
        self.assertEqual(value['status'],'infra_failure')

    def test_interleaved_progress_does_not_make_wrong_failure(self):
        report={'x':{'resolved':False,'tests_status':{'FAIL_TO_PASS':{'failure':['real','actually_ok']}}}}
        value=normalize(report,'real ... actually_ok ... ok\nFAIL: real (Suite)\nAssertionError: bad\n',case_id='x',exit_code=0)
        self.assertEqual(value['failure_id'],'real (Suite)')
        self.assertIn('AssertionError',value['log_excerpt'])

    def test_pollution_rejected_without_hiding_source(self):
        self.assertEqual(rejected_paths('+++ b/tests/new_test.py\n+++ b/django/forms/source.py\n'),['tests/new_test.py'])

    def test_idempotency_and_repeated_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=Ledger(directory,max_submissions=9,repeated_limit=2)
            feedback={'status':'product_failure','failure_id':'same','resolved':False}
            first=ledger.record('id1','key','sha',feedback)
            self.assertEqual(first,ledger.record('id1','key','sha',feedback))
            second=ledger.record('id2','key','sha',feedback,cache_hit=True)
            self.assertEqual(len(ledger.entries),2)
            self.assertEqual(second['feedback']['terminal'],'stagnated')
            self.assertEqual(Ledger(directory).entries,ledger.entries)

    def test_infra_does_not_increment_product_stagnation(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=Ledger(directory)
            entry=ledger.record('id','key','sha',{'status':'infra_failure','resolved':False,'failure_id':None})
            self.assertEqual(entry['feedback']['same_failure_count'],0)

    def test_success_uses_official_boolean(self):
        self.assertTrue(normalize({'x':{'resolved':True}},'',case_id='x',exit_code=0)['resolved'])

    def test_sympy_native_exception_is_returned(self):
        output=('test_Mod E\n________________ sympy/core/tests/test_arit.py:test_Mod ________________\n'
                'Traceback (most recent call last):\n  File "/testbed/sympy/core/mod.py", line 169, in eval\n'
                '    G = gcd(p, q)\nsympy.polys.polyerrors.PolynomialError: Piecewise generators do not make sense\n'
                ' tests finished: 92 passed, 1 exceptions\n')
        value=normalize({'x':{'resolved':False}},output,case_id='x',exit_code=0)
        self.assertEqual(value['failure_id'],'test_Mod')
        self.assertIn('PolynomialError: Piecewise',value['log_excerpt'])
        self.assertNotIn('feedback_uncertain',value)

    def test_long_traceback_keeps_exception_and_only_first_failure(self):
        output='FAIL: first (Suite)\n'+('  middle frame\n'*80)+'AssertionError: important cause\n'+'='*60+'\nFAIL: second (Suite)\n'
        value=normalize({'x':{'resolved':False}},output,case_id='x',exit_code=0)
        self.assertIn('important cause',value['log_excerpt'])
        self.assertNotIn('FAIL: second',value['log_excerpt'])

    def test_pytest_summary_fallback(self):
        value=normalize({'x':{'resolved':False}},'FAILED tests/foo.py::test_one - ValueError: bad\n',case_id='x',exit_code=0)
        self.assertEqual(value['failure_id'],'tests/foo.py::test_one')
        self.assertIn('ValueError',value['log_excerpt'])

    def test_paper_profile_disables_practical_resource_caps(self):
        from bench_tools.swebench_runner.controller import apply_budget_profile
        args=apply_budget_profile(SimpleNamespace(paper_budget=True,max_submissions=4,token_limit=300000,max_rewinds=6,timeout=2400))
        self.assertEqual(args.max_submissions,sys.maxsize)
        self.assertEqual(args.token_limit,sys.maxsize)
        self.assertEqual(args.max_rewinds,sys.maxsize)
        self.assertEqual(args.timeout,10**12)

    def test_bounded_profile_preserves_pilot_limits(self):
        from bench_tools.swebench_runner.controller import apply_budget_profile
        args=apply_budget_profile(SimpleNamespace(paper_budget=False,max_submissions=4,token_limit=300000,max_rewinds=6,timeout=2400))
        self.assertEqual((args.max_submissions,args.token_limit,args.max_rewinds,args.timeout),(4,300000,6,2400))

if __name__=='__main__':unittest.main()
