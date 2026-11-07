from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modules.composition import plan_goal, run_plan, validate_plan
from modules.pipeline_contracts import PipelinePlan
from modules.runtime import load_module_registry
from tool_runtime import ToolRuntime, ToolRuntimeError


class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='system-agentic-composition-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.inventory = [{**m, 'execution_ready': m['adapter_status'] == 'implemented'} for m in load_module_registry()['modules']]
        self.requests = [
            {'id':'export', 'capability':'pedagogy.session', 'task':{'context':{'pedagogy_request':{'action':'export','format':'md','session_id':'synthetic'}}}, 'expected':{'domain_outcome':'ARTIFACT_EXPORTED'}},
            {'id':'compile', 'capability':'knowledge.pack-compile', 'dependsOn':['export'],
             'inputBindings':[{'from':'export:export','to':'knowledge_sources'}],
             'task':{'execution_policy':'WORKSPACE_WRITE','context':{'knowledge_output':'pack'}}, 'expected':{'validation_status':'VALID'}},
        ]
        self.calls = []

    def plan(self, requests=None, workflow_id='fixture'):
        return plan_goal('Export a saved rehearsal and compile a verified source pack', requests or self.requests,
                         workspace=str(self.root), execution_policy='WORKSPACE_WRITE', workflow_id=workflow_id, inventory=self.inventory)

    def execute(self, task, *, module_id, module_capability):
        self.calls.append(task)
        if module_id == 'pedagogy':
            return {'task_id':task.task_id,'execution_status':'COMPLETED','domain_outcome':'ARTIFACT_EXPORTED',
                    'artifacts':[{'type':'pedagogy-session-export','format':'md','session_id':'synthetic','content':'# Authored rehearsal\nNo competence certification.\n'}]}
        source = self.root / task.context['knowledge_sources'][0]
        pack = self.root / 'pack'
        pack.mkdir()
        (pack/'source.md').write_bytes(source.read_bytes())
        (pack/'index.json').write_text('{}')
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        return {'task_id':task.task_id,'status':'COMPLETED','provider':'test-only',
                'execution_context':{'module':{'knowledge_workflow':{'output_root':str(pack),'validation_status':'VALID',
                'sources':[{'uri':'source.md','sha256':digest,'trust_tier':'draft','provenance':{'origin':'synthetic'}}]}}}}

    def run_fixture(self, plan=None, executor=None):
        with patch('modules.composition.module_inventory', return_value=self.inventory), patch('modules.composition.dispatch_task', side_effect=executor or self.execute):
            return run_plan(plan or self.plan())

    def test_roundtrip_preserves_existing_contract_and_explicit_bindings(self):
        plan = self.plan()
        rebuilt = PipelinePlan.from_dict(plan.to_dict())
        self.assertEqual(rebuilt, plan)
        self.assertEqual(plan.steps[1].input_refs, ('export:export',))

    def test_two_step_handoff_hash_provenance_and_completed_reuse(self):
        result = self.run_fixture()
        self.assertEqual(result['workflow_status'], 'COMPLETED')
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(result['verification'].values()))
        self.assertTrue(self.calls[1].context['composition_inputs'][0]['metadata']['sha256'])
        self.assertTrue(self.run_fixture()['reused'])
        self.assertEqual(len(self.calls), 2)

    def test_changed_artifact_rejects_reuse_without_duplicate(self):
        result = self.run_fixture()
        Path(result['artifacts'][0]['files'][0]).write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'already attempted'):
            self.run_fixture()
        self.assertEqual(len(self.calls), 2)

    def test_workspace_relative_result_uri_preserves_hashed_pack_evidence(self):
        def workspace_uri(task, **kw):
            result = self.execute(task, **kw)
            if kw['module_id'] == 'knowledge-builder':
                result['execution_context']['module']['knowledge_workflow']['sources'][0]['uri'] = 'pack/source.md'
            return result
        self.assertEqual(self.run_fixture(executor=workspace_uri)['workflow_status'], 'COMPLETED')

    def test_missing_export_keeps_completed_execution_and_blocks_consumer(self):
        result = self.run_fixture(executor=lambda task, **kw: {'task_id':task.task_id,'execution_status':'COMPLETED','domain_outcome':'INPUT_REJECTED','artifacts':[]})
        self.assertEqual(result['steps'][0]['execution_status'], 'COMPLETED')
        self.assertEqual(result['steps'][1]['execution_status'], 'BLOCKED')
        self.assertEqual(result['workflow_status'], 'BLOCKED')

    def test_failed_or_unknown_producer_never_dispatches_consumer(self):
        for status in ('FAILED', 'UNKNOWN'):
            calls = []
            def fail(task, **kw):
                calls.append(task)
                return {'task_id':task.task_id, 'status':status}
            result = self.run_fixture(self.plan(workflow_id=status), fail)
            self.assertEqual(len(calls), 1)
            self.assertEqual(result['steps'][1]['execution_status'], 'BLOCKED')

    def test_invalid_domain_outcome_does_not_mean_infrastructure_failed(self):
        def invalid(task, **kw):
            result = self.execute(task, **kw)
            if kw['module_id'] == 'knowledge-builder':
                result['execution_context']['module']['knowledge_workflow']['validation_status'] = 'INVALID'
            return result
        result = self.run_fixture(executor=invalid)
        self.assertEqual(result['steps'][1]['execution_status'], 'COMPLETED')
        self.assertEqual(result['steps'][1]['validation_status'], 'INVALID')
        self.assertEqual(result['workflow_status'], 'BLOCKED')

    def test_forged_hash_metadata_cannot_replace_actual_evidence(self):
        def forge(task, **kw):
            result = self.execute(task, **kw)
            if kw['module_id'] == 'knowledge-builder':
                (self.root/'pack/source.md').write_text('different bytes')
            return result
        result = self.run_fixture(executor=forge)
        self.assertFalse(result['steps'][1]['verification']['provenance_preserved'])
        self.assertEqual(result['workflow_status'], 'BLOCKED')

    def test_single_module_goals_stay_single(self):
        for cap in ('software.code-review','pedagogy.rehearse'):
            plan = self.plan([{'capability':cap,'task':{'context':{'pedagogy_request':{'action':'create','case_id':'fraction-magnitude'}}}}])
            self.assertFalse(plan.to_dict()['composition_required'])
            self.assertEqual(self.run_fixture(plan)['workflow_status'], 'NOT_REQUIRED')
        self.assertEqual(self.calls, [])

    def test_incompatible_or_undeclared_binding_refused(self):
        req = copy.deepcopy(self.requests)
        req[1]['capability'] = 'pedagogy.rehearse'
        with self.assertRaises(ValueError):
            self.plan(req)
        req = copy.deepcopy(self.requests)
        req[1]['inputBindings'][0]['from'] = 'export:absent'
        with self.assertRaisesRegex(ValueError, 'Incompatible'):
            self.plan(req)

    def test_live_availability_and_dependency_order_are_authoritative(self):
        self.inventory[3]['execution_ready'] = False
        with self.assertRaises(ValueError):
            self.plan()
        self.inventory[3]['execution_ready'] = True
        req = copy.deepcopy(self.requests)
        req[1]['dependsOn'] = []
        with self.assertRaises(ValueError):
            self.plan(req)

    def test_elevated_worker_origin_and_task_override_fail_before_execution(self):
        plan = self.plan()
        raw = plan.to_dict()
        raw['execution_policy'] = 'ELEVATED'
        with self.assertRaises(ValueError):
            validate_plan(PipelinePlan.from_dict(raw), self.inventory)
        with patch.dict(os.environ, {'SYSTEM_AGENTIC_EXECUTION_ROLE':'worker'}), self.assertRaisesRegex(ValueError, 'RECURSION'):
            validate_plan(plan, self.inventory)
        req = copy.deepcopy(self.requests)
        req[0]['task']['workspace'] = '/tmp/elsewhere'
        with self.assertRaises(ValueError):
            self.plan(req)

    def test_input_binding_cannot_overwrite_existing_context(self):
        req = copy.deepcopy(self.requests)
        req[1]['task']['context']['knowledge_sources'] = ['unrelated.md']
        with self.assertRaisesRegex(ValueError, 'overwrite'):
            self.plan(req)

    def test_artifact_writer_enforces_policy_no_overwrite_no_symlink_and_bounds(self):
        with self.assertRaises(ToolRuntimeError):
            ToolRuntime(self.root).materialize_artifact('export.md', b'text')
        tools = ToolRuntime(self.root, 'WORKSPACE_WRITE')
        tools.materialize_artifact('evidence/export.md', b'exact')
        with self.assertRaises(FileExistsError):
            tools.materialize_artifact('evidence/export.md', b'changed')
        for path in ('../escape.md', '/tmp/escape.md', '.env'):
            with self.assertRaises(ToolRuntimeError):
                tools.materialize_artifact(path, b'text')
        (self.root/'linked').symlink_to(self.root/'evidence', target_is_directory=True)
        with self.assertRaises(OSError):
            tools.materialize_artifact('linked/other.md', b'text')
        self.assertEqual((self.root/'evidence/export.md').read_bytes(), b'exact')


if __name__ == '__main__':
    unittest.main()
