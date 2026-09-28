"""Inventory covers the existing registry; it never creates a registration."""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[4]
FACTORY = ROOT / 'modules/agent-factory'

class InventoryTest(unittest.TestCase):
    def test_every_registered_persona_is_explicit_and_sources_exist(self):
        spec = importlib.util.spec_from_file_location('projection_registry', FACTORY / 'webhook-ingress/lambda/common/personas.py')
        registry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(registry)
        inventory = json.loads((FACTORY / 'codex-harness/persona-projection-inventory.json').read_text())
        rows = inventory['personas']
        self.assertEqual({row['registeredPersona'] for row in rows}, registry.VALID_PERSONAS)
        self.assertEqual(len(rows), len(registry.VALID_PERSONAS))
        for row in rows:
            self.assertFalse(row['qualifiedByThisChange'])
            if row['registeredPersonaSource']:
                self.assertTrue((ROOT / row['registeredPersonaSource']).is_file())
            else:
                self.assertEqual(row['registeredPersona'], 'pt-superpower')
            if row['definition']:
                definition = json.loads((ROOT / row['definition']).read_text())
                self.assertEqual(row['codexKey'], definition['key'])
                self.assertEqual(row['requiredCapabilities'], definition['requiredCapabilities'])
                self.assertTrue((ROOT / row['sharedPersonaSource']).is_file())

if __name__ == '__main__':
    unittest.main()
