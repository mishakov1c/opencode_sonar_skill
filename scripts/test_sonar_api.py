"""Синтетические тесты API-приёма, без реальной истории и сети."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sonar_ingest as intake


MARKER = 'abcd0123456789abcdef012345'
SOURCE = 'http://sonar.invalid/project/issues?id=example&pullRequest=7'
PAYLOAD = {'total': 0, 'paging': {'total': 0, 'pageIndex': 1, 'pageSize': 100},
           'issues': [], 'components': [{'key': 'example', 'qualifier': 'TRK', 'pullRequest': '7'}]}


def tool(command, status, output=''):
    return {'type': 'tool', 'name': 'shell', 'state': {
        'status': status, 'input': {'command': command},
        'content': [{'type': 'text', 'text': output}]}}


def fixture(status='running'):
    return [
        {'id': 'm1', 'type': 'user', 'text': SOURCE},
        {'id': 'm2', 'type': 'assistant', 'content': [
            tool('python scripts/sonar_index.py api-url "' + SOURCE + '"', 'completed',
                 intake.api_url(SOURCE) + '\nWAITING_FOR_USER_JSON')]},
        {'id': 'm3', 'type': 'user', 'text': json.dumps(PAYLOAD)},
        {'id': 'm4', 'type': 'assistant', 'content': [
            tool('python scripts/sonar_ingest.py --call-id ' + MARKER, status)]}]


class ApiTests(unittest.TestCase):
    def run_pages(self, pages):
        responses = []
        for page in pages:
            response = unittest.mock.Mock(returncode=0, stdout=json.dumps(page))
            responses.append(response)
        with patch.dict(os.environ, {'OPENCODE_SESSION_ID': 'ses_test'}), \
                patch.object(intake, 'api_cli', return_value='cli.exe'), \
                patch.object(intake.subprocess, 'run', side_effect=responses) as run:
            result = intake.api_history(MARKER)
            return result, run.call_args_list

    def test_paginated_ingestion(self):
        data = fixture()
        (sid, messages), calls = self.run_pages([
            {'data': data[:2], 'cursor': {'next': 'opaque+/='}},
            {'data': data[2:], 'cursor': {}}])
        self.assertEqual(sid, 'ses_test')
        self.assertIn('cursor=opaque%2B%2F%3D', calls[1].args[0][-1])
        self.assertNotIn('order=', calls[1].args[0][-1])
        with tempfile.TemporaryDirectory() as root:
            result = intake.ingest(None, sid, root, messages)
            self.assertEqual(result['status'], 'READY')
            self.assertEqual(result['total'], 0)
            self.assertTrue(Path(result['statePath']).is_file())

    def test_completed_call_rejected(self):
        with self.assertRaises(intake.IntakeError):
            self.run_pages([{'data': fixture('completed'), 'cursor': {}}])

    def test_duplicate_call_rejected(self):
        data = fixture()
        data.append({'id': 'm5', 'type': 'assistant', 'content': data[-1]['content']})
        with self.assertRaises(intake.IntakeError):
            self.run_pages([{'data': data, 'cursor': {}}])

    def test_cursor_loop_rejected(self):
        with self.assertRaises(intake.IntakeError):
            self.run_pages([{'data': fixture(), 'cursor': {'next': 'x'}},
                            {'data': [], 'cursor': {'next': 'x'}}])

    def test_wrong_project_rejected(self):
        data = fixture()
        payload = dict(PAYLOAD, components=[{'key': 'other', 'qualifier': 'TRK', 'pullRequest': '7'}])
        data[2]['text'] = json.dumps(payload)
        (sid, messages), _ = self.run_pages([{'data': data, 'cursor': {}}])
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(intake.IntakeError):
                intake.ingest(None, sid, root, messages)
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_bad_session_rejected_before_cli(self):
        with patch.dict(os.environ, {'OPENCODE_SESSION_ID': '../wrong'}), \
                patch.object(intake, 'api_cli') as cli:
            with self.assertRaises(intake.IntakeError):
                intake.api_history(MARKER)
            cli.assert_not_called()


if __name__ == '__main__':
    unittest.main()
