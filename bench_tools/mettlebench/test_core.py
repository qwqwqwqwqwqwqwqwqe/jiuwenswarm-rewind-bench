import os,tempfile,unittest
from pathlib import Path
from .core import normalize,tree_hash

class AdapterTests(unittest.TestCase):
    def test_only_first_feedback_is_exposed(self):
        result=normalize({'status':'fail','pass_rate':.5,'details':['SECRET_HIDDEN_CHECK'],
            'first_failure':{'id':'P03','feedback':'Observed output is invalid'}})
        self.assertEqual(result['failure_id'],'P03')
        self.assertNotIn('SECRET',str(result))
        self.assertNotIn('pass_rate',result)
        self.assertEqual(normalize({'status':'broken'})['status'],'infra_failure')
    def test_snapshot_identity_covers_git_data_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'.git').mkdir();head=root/'.git/HEAD';head.write_text('ref: refs/heads/main')
            data=root/'data.bin';data.write_bytes(b'original');os.symlink('data.bin',root/'link')
            initial=tree_hash(root);self.assertEqual(initial,tree_hash(root))
            head.write_text('ref: refs/heads/other');self.assertNotEqual(initial,tree_hash(root))
            head.write_text('ref: refs/heads/main');self.assertEqual(initial,tree_hash(root))
            data.chmod(0o400);self.assertNotEqual(initial,tree_hash(root))
if __name__=='__main__':unittest.main()
