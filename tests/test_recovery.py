from types import SimpleNamespace
import unittest
from unittest import mock
import test_manager as previous
from fleet_control.manager import ManagedFleetController

class Recovery(unittest.TestCase):
    fixture = previous.ManagerTests.fixture
    git = previous.ManagerTests.git
    def check(self, history, expected, selected='new', adopted=False):
        temporary, config, head, log = self.fixture(managed_attempt=True)
        with temporary:
            controller = ManagedFleetController(config_path=config)
            for kind, identity, extras in history:
                fact = {'order_id':'order','task_id':'task','base_sha':head,**extras}
                if identity is not None: fact['attempt_id']=identity
                controller.journal.append(kind, fact)
            controller.inventory_observation=SimpleNamespace(sessions=[SimpleNamespace(terminal=False,id='session',attempt_id=None if adopted else selected,task_id='task',order_id='order',base_sha=head,status='running')])
            adoptions = {'session':SimpleNamespace(approved=True,base_sha=head,task_id='task',order_id='order')} if adopted else {}
            with mock.patch('fleet_control.manager.load_adoptions',return_value=adoptions):
                actions=controller.reconcile_sessions(head=head,now=100)
            self.assertEqual(bool(actions),expected,actions)
            self.assertFalse(log.exists())
    def test_replacement(self):
        for kind in ('attempt.failed','attempt.refused','attempt.cancelled','attempt.rejected'):
            with self.subTest(kind=kind):
                self.check([('attempt.started','old',{}),(kind,'old',{}),('attempt.started','new',{})],False)
    def test_late(self):
        self.check([('attempt.started','old',{}),('attempt.failed','old',{}),('attempt.started','new',{}),('attempt.refused','old',{})],False)
    def test_holds(self):
        for kind in ('attempt.ready','attempt.admitted','attempt.no-change','attempt.reverted'):
            with self.subTest(kind=kind):
                self.check([('attempt.started','old',{}),(kind,'old',{}),('attempt.started','new',{})],True)
    def test_review(self):
        self.check([('attempt.started','old',{}),('attempt.ready','old',{}),('attempt.rejected','old',{}),('attempt.started','new',{})],False)
    def test_old(self):
        self.check([('attempt.started','old',{}),('attempt.failed','old',{}),('attempt.started','new',{})],True,selected='old')
    def test_duplicate(self):
        self.check([('attempt.started','old',{}),('attempt.failed','old',{}),('attempt.started','new',{}),('attempt.started','new',{})],True)
    def test_missing(self):
        self.check([('attempt.failed','old',{}),('attempt.started','new',{})],True)
    def test_unbound(self):
        self.check([('attempt.failed',None,{}),('attempt.started','new',{})],True)
    def test_route(self):
        self.check([('attempt.refused',None,{'retryable_route_failure':True}),('attempt.started','new',{})],False)
    def test_pending(self):
        self.check([('attempt.started','old',{}),('attempt.started','other',{}),('attempt.failed','other',{}),('attempt.started','new',{})],True)
    def test_adopted(self):
        self.check([('attempt.started','old',{}),('attempt.failed','old',{}),('attempt.started','new',{})],True,adopted=True)
    def test_terminal(self):
        self.check([('attempt.started','old',{}),('attempt.failed','old',{}),('attempt.started','new',{}),('attempt.failed','new',{})],True)
