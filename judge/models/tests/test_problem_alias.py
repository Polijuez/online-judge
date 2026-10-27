import uuid

from django.core.exceptions import ValidationError
from django.db import connections
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase
from django.urls import reverse

from judge.models import Problem, ProblemAlias
from judge.models.tests.util import CommonDataMixin, create_problem, create_solution


class ProblemAliasRedirectTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        cls.problem = create_problem(
            code='6ba7b810-9dad-11d1-80b4-00c04fd430c8',
            name='Caballos salvajes',
            is_public=True,
        )
        cls.problem.add_alias('caballos')
        create_solution(problem=cls.problem)

    def test_alias_redirects_permanently_to_canonical_url(self):
        response = self.client.get('/problem/caballos')
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response['Location'], reverse('problem_detail', args=[self.problem.code]))

    def test_canonical_url_always_resolves(self):
        self.assertEqual(self.client.get('/problem/%s' % self.problem.code).status_code, 200)

    def test_alias_redirect_ignores_case(self):
        response = self.client.get('/problem/CABALLOS')
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response['Location'], reverse('problem_detail', args=[self.problem.code]))

    def test_alias_redirect_preserves_the_subpath(self):
        response = self.client.get('/problem/caballos/editorial')
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response['Location'], '/problem/%s/editorial' % self.problem.code)
        self.assertEqual(self.client.get(response['Location']).status_code, 200)

    def test_unknown_alias_is_a_404(self):
        self.assertEqual(self.client.get('/problem/inexistente').status_code, 404)

    def test_problem_without_alias_works_and_does_not_break_list_or_search(self):
        problem = create_problem(code=str(uuid.uuid4()), name='Problema sin alias', is_public=True)
        self.assertFalse(problem.aliases.exists())

        self.assertEqual(self.client.get(reverse('problem_detail', args=[problem.code])).status_code, 200)

        listing = self.client.get(reverse('problem_list'))
        self.assertEqual(listing.status_code, 200)
        self.assertIn(problem, list(listing.context['problems']))

        search = self.client.get(reverse('problem_list'), {'search': 'Problema sin alias'})
        self.assertEqual(search.status_code, 200)
        self.assertIn(problem, list(search.context['problems']))


class ProblemAliasTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        cls.problem = create_problem(code=str(uuid.uuid4()), name='Caballos', is_public=True)
        cls.other_problem = create_problem(code=str(uuid.uuid4()), name='Otro', is_public=True)
        cls.alias = cls.problem.add_alias('caballos')

    def test_alias_is_stored_lowercase(self):
        alias = self.other_problem.add_alias('Pompeya')
        self.assertEqual(alias.name, 'pompeya')
        self.assertEqual(ProblemAlias.objects.get(pk=alias.pk).name, 'pompeya')

    def test_duplicate_alias_with_other_casing_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            self.other_problem.add_alias('CABALLOS')
        self.assertIn('already in use', str(ctx.exception))
        self.assertFalse(self.other_problem.aliases.exists())

    def test_duplicate_alias_on_the_same_problem_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.problem.add_alias('Caballos')

    def test_invalid_alias_is_rejected(self):
        for name in ('dos caballos', 'ñandú', 'x' * 41, ''):
            with self.subTest(name=name):
                with self.assertRaises(ValidationError):
                    self.other_problem.add_alias(name)

    def test_aliases_can_be_listed(self):
        self.assertCountEqual(self.problem.aliases.values_list('name', flat=True), ['caballos'])
        self.assertCountEqual(ProblemAlias.objects.filter(problem=self.problem).values_list('name', flat=True),
                              ['caballos'])
        self.assertEqual(ProblemAlias.resolve('CABALLOS'), self.alias)

    def test_adding_an_alias_does_not_replace_the_existing_ones(self):
        self.problem.add_alias('caballos-salvajes')
        self.assertCountEqual(self.problem.aliases.values_list('name', flat=True),
                              ['caballos', 'caballos-salvajes'])
        self.assertEqual(self.client.get('/problem/caballos').status_code, 301)

    def test_alias_keeps_redirecting_after_the_problem_is_renamed(self):
        self.problem.name = 'Caballos del sur'
        self.problem.save()
        self.assertTrue(ProblemAlias.objects.filter(name='caballos', problem=self.problem).exists())
        response = self.client.get('/problem/caballos')
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response['Location'], reverse('problem_detail', args=[self.problem.code]))

    def test_alias_does_not_change_the_problem_id(self):
        before = self.problem.code
        ProblemAlias.create_for(self.problem, 'otro-alias')
        self.assertEqual(Problem.objects.get(pk=self.problem.pk).code, before)


class AliasMigrationOrderTestCase(TestCase):
    def test_alias_table_is_created_before_the_uuid_backfill(self):
        loader = MigrationLoader(connections['default'], ignore_no_migrations=True)

        alias_node = loader.graph.node_map[('judge', '0157_problem_alias')]
        backfill_node = loader.graph.node_map[('judge', '0158_problem_uuid')]

        self.assertEqual({node.key for node in backfill_node.parents}, {('judge', '0157_problem_alias')})
        self.assertIn(('judge', '0156_problemdata_evaluator_problemdata_signature_and_more'),
                      {node.key for node in alias_node.parents})
