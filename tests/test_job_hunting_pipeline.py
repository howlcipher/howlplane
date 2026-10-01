import unittest

from tests._requirements import import_or_skip

import_or_skip("litellm")
from unittest.mock import patch, mock_open, MagicMock
import json
import sys
import pathlib

# Add repo root to path to import the script
repo_root = pathlib.Path(__file__).resolve().parents[1]
sys.path.append(str(repo_root))

from scripts.job_hunting_pipeline import load_user_profile, fetch_job_postings, generate_application_materials, save_materials, main

class TestJobHuntingPipeline(unittest.TestCase):
    
    @patch("builtins.open", new_callable=mock_open, read_data="Mock Profile Data")
    def test_load_user_profile(self, mock_file):
        profile = load_user_profile()
        self.assertEqual(profile, "Mock Profile Data")
        mock_file.assert_called_once()
        
    def test_fetch_job_postings(self):
        jobs = fetch_job_postings()
        self.assertTrue(isinstance(jobs, list))
        self.assertTrue(len(jobs) > 0)
        self.assertIn("id", jobs[0])
        
    @patch("scripts.job_hunting_pipeline.litellm.completion")
    def test_generate_application_materials(self, mock_completion):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps({
            "resume_md": "# Mock Resume",
            "cover_letter_md": "# Mock Cover Letter"
        })
        mock_completion.return_value = mock_response
        
        resume, cover_letter = generate_application_materials("profile", {"title": "T", "company": "C", "description": "D"}, model="mock-model")
        
        self.assertEqual(resume, "# Mock Resume")
        self.assertEqual(cover_letter, "# Mock Cover Letter")
        mock_completion.assert_called_once()
        
    @patch("pathlib.Path.mkdir")
    @patch("builtins.open", new_callable=mock_open)
    def test_save_materials(self, mock_file, mock_mkdir):
        save_materials("job_test", "resume data", "cover letter data")
        
        mock_mkdir.assert_called_once_with(parents=True, exist_ok=True)
        # Should open two files
        self.assertEqual(mock_file.call_count, 2)
        
    @patch("scripts.job_hunting_pipeline.save_materials")
    @patch("scripts.job_hunting_pipeline.generate_application_materials")
    @patch("scripts.job_hunting_pipeline.fetch_job_postings")
    @patch("scripts.job_hunting_pipeline.load_user_profile")
    def test_main(self, mock_load, mock_fetch, mock_generate, mock_save):
        mock_load.return_value = "Profile"
        mock_fetch.return_value = [{"id": "job1", "title": "T", "company": "C", "description": "D"}]
        mock_generate.return_value = ("resume", "cover letter")
        
        main()
        
        mock_load.assert_called_once()
        mock_fetch.assert_called_once()
        mock_generate.assert_called_once_with("Profile", mock_fetch.return_value[0])
        mock_save.assert_called_once_with("job1", "resume", "cover letter")

if __name__ == "__main__":
    unittest.main()
