from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from source.kg.build.pipeline import build_kg
from source.kg.core.repo_source import RepoSnapshot
from source.kg.core.store import JsonlKgStore
from source.kg.languages.python.extractors.ast_extractor import KgBuild, PythonAstExtractor
from source.kg.product.authz_surface import authz_surface_packet
from source.kg.product.framework_impact import framework_impact_packet
from source.kg.product.mcp_tools import call_tool
from source.kg.query.snapshot import KgSnapshot


class PythonDjangoFrameworkExtractionTest(unittest.TestCase):
    def test_django_model_serializer_viewset_and_task_emit_framework_impact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            models = app / "models.py"
            serializers = app / "serializers.py"
            views = app / "views.py"
            tasks = app / "tasks.py"
            models.write_text(
                "from django.db import models\n\n"
                "class Customer(models.Model):\n"
                "    email = models.EmailField()\n\n"
                "class Order(models.Model):\n"
                "    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)\n"
                "    total = models.DecimalField(max_digits=8, decimal_places=2)\n",
                encoding="utf-8",
            )
            serializers.write_text(
                "from rest_framework import serializers\n"
                "from .models import Order\n\n"
                "class OrderSerializer(serializers.ModelSerializer):\n"
                "    class Meta:\n"
                "        model = Order\n"
                "        fields = ['id', 'customer', 'total']\n",
                encoding="utf-8",
            )
            views.write_text(
                "from rest_framework.viewsets import ModelViewSet\n"
                "from .models import Order\n"
                "from .serializers import OrderSerializer\n\n"
                "class OrderViewSet(ModelViewSet):\n"
                "    queryset = Order.objects.all()\n"
                "    serializer_class = OrderSerializer\n",
                encoding="utf-8",
            )
            tasks.write_text(
                "from celery import shared_task\n"
                "from .models import Order\n\n"
                "@shared_task\n"
                "def send_order_receipt(order_id):\n"
                "    order = Order.objects.get(id=order_id)\n"
                "    return order.total\n",
                encoding="utf-8",
            )

            build = _build(root, (models, serializers, views, tasks))
            _assert_support_facts_reference_entities(build)
            kg = _snapshot(root, build)

        predicates = {fact.predicate for fact in build.support_facts}
        self.assertIn("DECLARES_FIELD", predicates)
        self.assertIn("RELATES_TO_MODEL", predicates)
        self.assertIn("SERIALIZES_MODEL", predicates)
        self.assertIn("HANDLES_MODEL", predicates)
        self.assertIn("TASK_USES_MODEL", predicates)

        result = call_tool(
            kg,
            "review_context",
            {
                "repo": "orders",
                "changed_files": ["orders/models.py"],
                "changed_ranges": [{"path": "orders/models.py", "start_line": 6, "end_line": 8}],
                "include_broad_context": True,
            },
        )

        self.assertEqual(result["status"], "found")
        impact = result["framework_impact"]
        self.assertEqual(impact["status"], "found")
        self.assertEqual(impact["summary"]["changed_framework_model_count"], 1)
        self.assertGreaterEqual(impact["summary"]["model_field_count"], 2)
        self.assertEqual(impact["serializers"][0]["subject"]["qualname"], "OrderSerializer")
        self.assertEqual(impact["views"][0]["subject"]["qualname"], "OrderViewSet")
        self.assertEqual(impact["tasks"][0]["subject"]["qualname"], "send_order_receipt")

        limited = call_tool(
            kg,
            "review_context",
            {
                "repo": "orders",
                "changed_files": ["orders/models.py"],
                "changed_ranges": [{"path": "orders/models.py", "start_line": 6, "end_line": 8}],
                "limit": 1,
                "include_broad_context": True,
            },
        )
        self.assertEqual(limited["summary"]["framework_model_count"], 1)
        self.assertEqual(len(limited["framework_impact"]["model_fields"]), 1)
        self.assertGreater(limited["framework_impact"]["summary"]["model_field_count"], 1)

        serializer_change = call_tool(
            kg,
            "review_context",
            {
                "repo": "orders",
                "changed_files": ["orders/serializers.py"],
                "changed_ranges": [{"path": "orders/serializers.py", "start_line": 4, "end_line": 7}],
                "include_broad_context": True,
            },
        )
        self.assertEqual(serializer_change["framework_impact"]["status"], "found")
        self.assertEqual(serializer_change["framework_impact"]["summary"]["changed_framework_model_count"], 1)
        self.assertEqual(serializer_change["framework_impact"]["changed_models"][0]["qualname"], "Order")

    def test_dynamic_serializer_model_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            models = app / "models.py"
            serializers = app / "serializers.py"
            models.write_text(
                "from django.db import models\n\n"
                "class Order(models.Model):\n"
                "    total = models.IntegerField()\n",
                encoding="utf-8",
            )
            serializers.write_text(
                "from rest_framework import serializers\n"
                "from .models import Order\n\n"
                "MODEL = Order\n\n"
                "class OrderSerializer(serializers.ModelSerializer):\n"
                "    class Meta:\n"
                "        model = MODEL\n"
                "        fields = '__all__'\n",
                encoding="utf-8",
            )

            build = _build(root, (models, serializers))

        serializer_facts = [fact for fact in build.support_facts if fact.predicate == "SERIALIZES_MODEL"]
        self.assertEqual(serializer_facts, [])

    def test_unrelated_app_task_decorator_does_not_emit_task_fact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            models = app / "models.py"
            tasks = app / "tasks.py"
            models.write_text(
                "from django.db import models\n\n"
                "class Order(models.Model):\n"
                "    total = models.IntegerField()\n",
                encoding="utf-8",
            )
            tasks.write_text(
                "from django.conf import settings\n"
                "from .models import Order\n\n"
                "@app.task\n"
                "def purge_order(order_id):\n"
                "    return Order.objects.get(id=order_id).total\n",
                encoding="utf-8",
            )

            build = _build(root, (models, tasks))
            _assert_support_facts_reference_entities(build)

        task_facts = [fact for fact in build.support_facts if fact.predicate == "TASK_USES_MODEL"]
        self.assertEqual(task_facts, [])

    def test_async_celery_task_uses_main_extractor_symbol_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            models = app / "models.py"
            tasks = app / "tasks.py"
            models.write_text(
                "from django.db import models\n\n"
                "class Order(models.Model):\n"
                "    total = models.IntegerField()\n",
                encoding="utf-8",
            )
            tasks.write_text(
                "from celery import shared_task\n"
                "from .models import Order\n\n"
                "@shared_task\n"
                "async def refresh_order(order_id):\n"
                "    order = Order.objects.get(id=order_id)\n"
                "    return order.total\n",
                encoding="utf-8",
            )

            build = _build(root, (models, tasks))

        task_facts = [fact for fact in build.support_facts if fact.predicate == "TASK_USES_MODEL"]
        self.assertEqual(len(task_facts), 1)
        entities_by_id = {entity.entity_id: entity for entity in build.entities}
        task_entity = entities_by_id[task_facts[0].subject_id]
        self.assertEqual(task_entity.identity["qualname"], "refresh_order")
        self.assertEqual(task_entity.identity["symbol_kind"], "async_function")

    def test_nested_celery_task_is_not_linked_to_top_level_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            models = app / "models.py"
            tasks = app / "tasks.py"
            models.write_text(
                "from django.db import models\n\n"
                "class Order(models.Model):\n"
                "    total = models.IntegerField()\n",
                encoding="utf-8",
            )
            tasks.write_text(
                "from celery import shared_task\n"
                "from .models import Order\n\n"
                "def refresh_order():\n"
                "    return None\n\n"
                "def register_tasks():\n"
                "    @shared_task\n"
                "    def refresh_order(order_id):\n"
                "        return Order.objects.get(id=order_id).total\n"
                "    return refresh_order\n",
                encoding="utf-8",
            )

            build = _build(root, (models, tasks))

        task_facts = [fact for fact in build.support_facts if fact.predicate == "TASK_USES_MODEL"]
        self.assertEqual(task_facts, [])

    def test_viewset_serializer_import_resolves_when_short_name_is_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            orders = root / "orders"
            archive = root / "archive"
            orders.mkdir()
            archive.mkdir()
            order_models = orders / "models.py"
            order_serializers = orders / "serializers.py"
            order_views = orders / "views.py"
            archive_models = archive / "models.py"
            archive_serializers = archive / "serializers.py"
            order_models.write_text(
                "from django.db import models\n\n"
                "class Order(models.Model):\n"
                "    total = models.IntegerField()\n",
                encoding="utf-8",
            )
            order_serializers.write_text(
                "from rest_framework import serializers\n"
                "from .models import Order\n\n"
                "class OrderSerializer(serializers.ModelSerializer):\n"
                "    class Meta:\n"
                "        model = Order\n"
                "        fields = '__all__'\n",
                encoding="utf-8",
            )
            order_views.write_text(
                "from rest_framework.viewsets import ModelViewSet\n"
                "from .serializers import OrderSerializer\n\n"
                "class OrderViewSet(ModelViewSet):\n"
                "    serializer_class = OrderSerializer\n",
                encoding="utf-8",
            )
            archive_models.write_text(
                "from django.db import models\n\n"
                "class ArchivedOrder(models.Model):\n"
                "    total = models.IntegerField()\n",
                encoding="utf-8",
            )
            archive_serializers.write_text(
                "from rest_framework import serializers\n"
                "from .models import ArchivedOrder\n\n"
                "class OrderSerializer(serializers.ModelSerializer):\n"
                "    class Meta:\n"
                "        model = ArchivedOrder\n"
                "        fields = '__all__'\n",
                encoding="utf-8",
            )

            build = _build(
                root,
                (
                    order_models,
                    order_serializers,
                    order_views,
                    archive_models,
                    archive_serializers,
                ),
            )

        view_facts = [fact for fact in build.support_facts if fact.predicate == "HANDLES_MODEL"]
        self.assertEqual(len(view_facts), 1)
        model_by_id = {entity.entity_id: entity for entity in build.entities}
        self.assertEqual(model_by_id[view_facts[0].object_id].identity["qualname"], "Order")

    def test_build_kg_writes_framework_support_facts_outside_canonical_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            root.mkdir()
            (root / "models.py").write_text(
                "from django.db import models\n\n"
                "class Order(models.Model):\n"
                "    total = models.IntegerField()\n",
                encoding="utf-8",
            )
            out = Path(tmpdir) / "snapshot"

            manifest = build_kg(root, out)
            facts_text = (out / "facts.jsonl").read_text(encoding="utf-8")
            support_text = (out / "support_facts.jsonl").read_text(encoding="utf-8")

            self.assertEqual(manifest["extractor_errors"], [])
            self.assertGreaterEqual(manifest["counts"]["support_facts"], 1)
            self.assertNotIn("DECLARES_FIELD", facts_text)
            self.assertIn("DECLARES_FIELD", support_text)

    def test_non_framework_repo_does_not_emit_django_specific_framework_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            module = root / "plain.py"
            module.write_text("def add(left, right):\n    return left + right\n", encoding="utf-8")

            build = _build(root, (module,))

        rows = [coverage for coverage in build.coverage if coverage.predicate == "FRAMEWORK_IMPACT"]
        self.assertEqual(rows, [])

    def test_framework_import_without_static_facts_emits_partial_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            module = root / "settings_reader.py"
            module.write_text(
                "from django.conf import settings\n\n"
                "def debug_enabled():\n"
                "    return settings.DEBUG\n",
                encoding="utf-8",
            )

            build = _build(root, (module,))
            kg = _snapshot(root, build)

        rows = [coverage for coverage in build.coverage if coverage.predicate == "FRAMEWORK_IMPACT"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].state, "partially_instrumented")
        self.assertEqual(rows[0].scope_ref["framework_family"], "python_framework_stack")
        self.assertEqual(rows[0].scope_ref["framework_import_roots"], ["django"])
        self.assertEqual(rows[0].scope_ref["reason"], "recognized_framework_without_static_framework_impact_facts")
        review = call_tool(
            kg,
            "review_context",
            {
                "repo": "orders",
                "changed_files": ["settings_reader.py"],
                "changed_ranges": [{"path": "settings_reader.py", "start_line": 3, "end_line": 4}],
                "include_broad_context": True,
            },
        )
        self.assertEqual(review["framework_impact"]["status"], "empty")

    def test_celery_only_import_uses_neutral_framework_family_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            module = root / "tasks.py"
            module.write_text(
                "from celery import shared_task\n\n"
                "@shared_task\n"
                "def ping():\n"
                "    return 'pong'\n",
                encoding="utf-8",
            )

            build = _build(root, (module,))

        rows = [coverage for coverage in build.coverage if coverage.predicate == "FRAMEWORK_IMPACT"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].scope_ref["framework_family"], "python_framework_stack")
        self.assertEqual(rows[0].scope_ref["framework_import_roots"], ["celery"])
        self.assertNotIn("framework", rows[0].scope_ref)

    def test_framework_impact_missing_repo_preserves_summary_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            models = app / "models.py"
            models.write_text(
                "from django.db import models\n\n"
                "class Order(models.Model):\n"
                "    total = models.IntegerField()\n",
                encoding="utf-8",
            )

            build = _build(root, (models,))
            kg = _snapshot(root, build)

        packet = framework_impact_packet(kg, repo="", changed_symbols=[], limit=5)

        self.assertEqual(packet["status"], "missing_repo")
        self.assertEqual(packet["summary"]["changed_framework_model_count"], 0)
        self.assertEqual(packet["summary"]["section_limit"], 5)

    def test_fieldless_model_change_impacts_serializer_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            models = app / "models.py"
            serializers = app / "serializers.py"
            models.write_text(
                "from django.db import models\n\n"
                "class Order(models.Model):\n"
                "    class Meta:\n"
                "        app_label = 'orders'\n",
                encoding="utf-8",
            )
            serializers.write_text(
                "from rest_framework import serializers\n"
                "from .models import Order\n\n"
                "class OrderSerializer(serializers.ModelSerializer):\n"
                "    class Meta:\n"
                "        model = Order\n"
                "        fields = '__all__'\n",
                encoding="utf-8",
            )

            build = _build(root, (models, serializers))
            kg = _snapshot(root, build)

        result = call_tool(
            kg,
            "review_context",
            {
                "repo": "orders",
                "changed_files": ["orders/models.py"],
                "changed_ranges": [{"path": "orders/models.py", "start_line": 3, "end_line": 5}],
                "include_broad_context": True,
            },
        )

        self.assertEqual(result["framework_impact"]["status"], "found")
        self.assertEqual(result["framework_impact"]["summary"]["changed_framework_model_count"], 1)
        self.assertEqual(result["framework_impact"]["changed_models"][0]["qualname"], "Order")

    def test_relationship_paths_stop_after_two_model_hops(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            models = app / "models.py"
            models.write_text(
                "from django.db import models\n\n"
                "class Country(models.Model):\n"
                "    name = models.CharField(max_length=50)\n\n"
                "class Customer(models.Model):\n"
                "    country = models.ForeignKey(Country, on_delete=models.CASCADE)\n\n"
                "class Order(models.Model):\n"
                "    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)\n\n"
                "class LineItem(models.Model):\n"
                "    order = models.ForeignKey(Order, on_delete=models.CASCADE)\n",
                encoding="utf-8",
            )

            build = _build(root, (models,))
            kg = _snapshot(root, build)

        result = call_tool(
            kg,
            "review_context",
            {
                "repo": "orders",
                "changed_files": ["orders/models.py"],
                "changed_ranges": [{"path": "orders/models.py", "start_line": 11, "end_line": 12}],
                "limit": 10,
                "include_broad_context": True,
            },
        )

        paths = result["framework_impact"]["relationship_paths"]
        self.assertTrue(paths)
        self.assertLessEqual(max(len(path["model_path"]) for path in paths), 3)
        self.assertTrue(all(path["relation"]["relation_type"] == "ForeignKey" for path in paths))
        self.assertEqual(result["framework_impact"]["summary"]["relationship_path_count"], 2)

    def test_drf_authz_surface_links_endpoint_handler_and_policies(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            views = app / "views.py"
            urls = app / "urls.py"
            views.write_text(
                "from rest_framework.permissions import BasePermission, IsAuthenticated\n"
                "from rest_framework.views import APIView\n\n"
                "class StaffOnly(BasePermission):\n"
                "    def has_permission(self, request, view):\n"
                "        return request.user.is_staff\n\n"
                "class OrderAdminView(APIView):\n"
                "    permission_classes = [IsAuthenticated, StaffOnly]\n"
                "    def post(self, request):\n"
                "        self.check_permissions(request)\n"
                "        return None\n",
                encoding="utf-8",
            )
            urls.write_text(
                "from django.urls import path\n"
                "from .views import OrderAdminView\n\n"
                "urlpatterns = [path('orders/admin/', OrderAdminView.as_view())]\n",
                encoding="utf-8",
            )

            build = _build(root, (views, urls))
            _assert_support_facts_reference_entities(build)
            kg = _snapshot(root, build)

        predicates = {fact.predicate for fact in build.support_facts}
        self.assertIn("DEFINES_AUTHZ_POLICY", predicates)
        self.assertIn("APPLIES_AUTHZ_POLICY", predicates)
        self.assertIn("USES_AUTHZ_CHECK", predicates)
        self.assertIn("HANDLES_ENDPOINT", predicates)

        result = call_tool(kg, "get_service_brief", {"service": "orders", "limit": 10})
        authz = result["authz_surface"]
        self.assertEqual(authz["status"], "found")
        self.assertEqual(authz["scope"], {"repo": "orders", "mode": "repo"})
        self.assertEqual(authz["summary"]["endpoint_handler_count"], 1)
        self.assertEqual(authz["answerability"]["missing_fact_families"], [])
        endpoint = authz["endpoint_authorization"][0]
        self.assertEqual(endpoint["endpoint"]["path"], "/orders/admin/")
        self.assertEqual(endpoint["handler"]["qualname"], "OrderAdminView")
        self.assertEqual(endpoint["authz_status"], "authz_evidence_found")
        policy_names = {row["qualifier"]["policy"] for row in endpoint["policies"]}
        self.assertIn("IsAuthenticated", policy_names)
        self.assertIn("StaffOnly", policy_names)
        staff_policy = next(row for row in endpoint["policies"] if row["qualifier"]["policy"] == "StaffOnly")
        self.assertEqual(staff_policy["object"]["kind"], "CodeSymbol")
        self.assertEqual(staff_policy["object"]["qualname"], "StaffOnly")

        fleet = call_tool(kg, "planning_context", {"limit": 10})
        self.assertEqual(fleet["related_facts"]["authz_surface"]["scope"]["mode"], "fleet")
        self.assertEqual(fleet["related_facts"]["authz_surface"]["summary"]["endpoint_handler_count"], 1)

        unscoped = authz_surface_packet(kg, repo=None, limit=10, allow_fleet=False)
        self.assertEqual(unscoped["scope"]["mode"], "unscoped")
        self.assertEqual(unscoped["summary"]["endpoint_handler_count"], 0)
        self.assertEqual(unscoped["answerability"]["missing_fact_families"], ["service_repo"])

    def test_drf_authz_surface_separates_public_from_missing_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            views = app / "views.py"
            urls = app / "urls.py"
            views.write_text(
                "from rest_framework.permissions import AllowAny, IsAuthenticated\n"
                "from rest_framework.views import APIView\n\n"
                "class PublicStatusView(APIView):\n"
                "    permission_classes = [AllowAny]\n"
                "    def get(self, request):\n"
                "        return None\n\n"
                "class MixedPolicyView(APIView):\n"
                "    permission_classes = [AllowAny, IsAuthenticated]\n"
                "    def get(self, request):\n"
                "        return None\n\n"
                "class MissingPolicyView(APIView):\n"
                "    def get(self, request):\n"
                "        return None\n",
                encoding="utf-8",
            )
            urls.write_text(
                "from django.urls import path\n"
                "from .views import MissingPolicyView, MixedPolicyView, PublicStatusView\n\n"
                "urlpatterns = [\n"
                "    path('status/', PublicStatusView.as_view()),\n"
                "    path('mixed/', MixedPolicyView.as_view()),\n"
                "    path('orders/missing/', MissingPolicyView.as_view()),\n"
                "]\n",
                encoding="utf-8",
            )

            build = _build(root, (views, urls))
            _assert_support_facts_reference_entities(build)
            kg = _snapshot(root, build)

        result = call_tool(kg, "get_service_brief", {"service": "orders", "limit": 10})
        by_path = {row["endpoint"]["path"]: row for row in result["authz_surface"]["endpoint_authorization"]}

        self.assertEqual(by_path["/status/"]["authz_status"], "declared_public")
        self.assertEqual(by_path["/mixed/"]["authz_status"], "authz_evidence_found")
        self.assertTrue(by_path["/mixed/"]["public_policy_present"])
        self.assertEqual(by_path["/orders/missing/"]["authz_status"], "missing_declared_policy")
        self.assertEqual(
            result["authz_surface"]["answerability"]["missing_fact_families"],
            ["endpoint_authz_policy"],
        )
        self.assertEqual(result["authz_surface"]["summary"]["missing_or_unknown_authz_count"], 1)

    def test_authz_surface_requires_framework_proof_for_authz_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            views = app / "views.py"
            urls = app / "urls.py"
            views.write_text(
                "from rest_framework.views import APIView\n\n"
                "class Helper:\n"
                "    def has_perm(self, name):\n"
                "        return True\n\n"
                "class LocalCheckPermissions:\n"
                "    def check_permissions(self, request):\n"
                "        return None\n"
                "    def run(self, request):\n"
                "        self.check_permissions(request)\n"
                "        return None\n\n"
                "def standalone_permission_probe(request):\n"
                "    return request.user.has_perm('orders.view_profile')\n\n"
                "class ProfileView(APIView):\n"
                "    def get(self, request):\n"
                "        helper = Helper()\n"
                "        return helper.has_perm('orders.view_profile')\n",
                encoding="utf-8",
            )
            urls.write_text(
                "from django.urls import path\n"
                "from .views import ProfileView\n\n"
                "urlpatterns = [path('profile/', ProfileView.as_view())]\n",
                encoding="utf-8",
            )

            build = _build(root, (views, urls))
            _assert_support_facts_reference_entities(build)
            kg = _snapshot(root, build)

        self.assertNotIn("USES_AUTHZ_CHECK", {fact.predicate for fact in build.support_facts})
        result = call_tool(kg, "get_service_brief", {"service": "orders", "limit": 10})
        endpoint = result["authz_surface"]["endpoint_authorization"][0]
        self.assertEqual(endpoint["authz_status"], "missing_declared_policy")
        self.assertEqual(endpoint["checks"], [])

    def test_authz_surface_extracts_custom_guard_call_with_auth_failure_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            views = app / "views.py"
            urls = app / "urls.py"
            api_key_validator = app / "api_key_validator.py"
            api_key_validator.write_text(
                "def get_company_by_api_key(request):\n"
                "    api_key = request.headers.get('api-key')\n"
                "    if api_key:\n"
                "        return object()\n",
                encoding="utf-8",
            )
            views.write_text(
                "from rest_framework import status\n"
                "from rest_framework.response import Response\n"
                "from rest_framework.views import APIView\n"
                "from orders.api_key_validator import get_company_by_api_key\n\n"
                "class PublicOrderView(APIView):\n"
                "    permission_classes = []\n"
                "    def post(self, request):\n"
                "        company = get_company_by_api_key(request)\n"
                "        if not company:\n"
                "            return Response({'error': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)\n"
                "        if not request.data.get('email'):\n"
                "            return Response({'error': 'Bad request'}, status=status.HTTP_400_BAD_REQUEST)\n"
                "        return None\n",
                encoding="utf-8",
            )
            urls.write_text(
                "from django.urls import path\n"
                "from .views import PublicOrderView\n\n"
                "urlpatterns = [path('public/orders/', PublicOrderView.as_view())]\n",
                encoding="utf-8",
            )

            build = _build(root, (api_key_validator, views, urls))
            _assert_support_facts_reference_entities(build)
            kg = _snapshot(root, build)

        result = call_tool(kg, "get_service_brief", {"service": "orders", "limit": 10})
        endpoint = result["authz_surface"]["endpoint_authorization"][0]
        self.assertEqual(endpoint["endpoint"]["path"], "/public/orders/")
        self.assertEqual(endpoint["authz_status"], "authz_evidence_found")
        self.assertEqual(len(endpoint["checks"]), 1)
        check = endpoint["checks"][0]
        self.assertEqual(check["qualifier"]["source_kind"], "custom_guard_call")
        self.assertEqual(check["qualifier"]["access_level"], "custom_security_guard")
        self.assertEqual(check["qualifier"]["guard_intent"], "unknown")
        self.assertEqual(check["qualifier"]["check"], "get_company_by_api_key")
        self.assertEqual(check["object"]["kind"], "CodeSymbol")
        self.assertEqual(check["object"]["module"], "orders.api_key_validator")
        self.assertEqual(check["object"]["properties"]["path"], "orders/api_key_validator.py")
        self.assertEqual(result["authz_surface"]["review_leads"][0]["lead_type"], "custom_guard")
        self.assertEqual(result["authz_surface"]["inspection_areas"][0]["area"], "custom_guard_implementations")

    def test_authz_surface_does_not_misattribute_unresolved_bare_guard_call_to_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            views = app / "views.py"
            urls = app / "urls.py"
            views.write_text(
                "from rest_framework import status\n"
                "from rest_framework.response import Response\n"
                "from rest_framework.views import APIView\n\n"
                "class PublicOrderView(APIView):\n"
                "    permission_classes = []\n"
                "    def post(self, request):\n"
                "        guard = request.guard\n"
                "        if not guard(request):\n"
                "            return Response({'error': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)\n"
                "        return None\n",
                encoding="utf-8",
            )
            urls.write_text(
                "from django.urls import path\n"
                "from .views import PublicOrderView\n\n"
                "urlpatterns = [path('public/orders/', PublicOrderView.as_view())]\n",
                encoding="utf-8",
            )

            build = _build(root, (views, urls))
            _assert_support_facts_reference_entities(build)
            kg = _snapshot(root, build)

        result = call_tool(kg, "get_service_brief", {"service": "orders", "limit": 10})
        endpoint = result["authz_surface"]["endpoint_authorization"][0]
        self.assertEqual(endpoint["checks"], [])

    def test_authz_surface_does_not_treat_arbitrary_forbidden_enum_as_status_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            views = app / "views.py"
            urls = app / "urls.py"
            views.write_text(
                "from rest_framework.response import Response\n"
                "from rest_framework.views import APIView\n\n"
                "class BillingTier:\n"
                "    FORBIDDEN = 403\n\n"
                "def resolve_company(request):\n"
                "    return object()\n\n"
                "class PublicOrderView(APIView):\n"
                "    permission_classes = []\n"
                "    def post(self, request):\n"
                "        company = resolve_company(request)\n"
                "        if not company:\n"
                "            return Response({'error': 'tier'}, status=BillingTier.FORBIDDEN)\n"
                "        return None\n",
                encoding="utf-8",
            )
            urls.write_text(
                "from django.urls import path\n"
                "from .views import PublicOrderView\n\n"
                "urlpatterns = [path('public/orders/', PublicOrderView.as_view())]\n",
                encoding="utf-8",
            )

            build = _build(root, (views, urls))
            _assert_support_facts_reference_entities(build)
            kg = _snapshot(root, build)

        result = call_tool(kg, "get_service_brief", {"service": "orders", "limit": 10})
        endpoint = result["authz_surface"]["endpoint_authorization"][0]
        self.assertEqual(endpoint["checks"], [])

    def test_authz_surface_extracts_custom_guard_with_positional_response_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            views = app / "views.py"
            urls = app / "urls.py"
            views.write_text(
                "from rest_framework.response import Response\n"
                "from rest_framework.views import APIView\n\n"
                "def resolve_company(request):\n"
                "    return object()\n\n"
                "class PublicOrderView(APIView):\n"
                "    permission_classes = []\n"
                "    def post(self, request):\n"
                "        company = resolve_company(request)\n"
                "        if not company:\n"
                "            return Response({'error': 'Unauthorized'}, 401)\n"
                "        return None\n",
                encoding="utf-8",
            )
            urls.write_text(
                "from django.urls import path\n"
                "from .views import PublicOrderView\n\n"
                "urlpatterns = [path('public/orders/', PublicOrderView.as_view())]\n",
                encoding="utf-8",
            )

            build = _build(root, (views, urls))
            _assert_support_facts_reference_entities(build)
            kg = _snapshot(root, build)

        result = call_tool(kg, "get_service_brief", {"service": "orders", "limit": 10})
        endpoint = result["authz_surface"]["endpoint_authorization"][0]
        self.assertEqual(endpoint["checks"][0]["qualifier"]["source_kind"], "custom_guard_call")

    def test_authz_surface_extracts_custom_guard_with_implicit_forbidden_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            views = app / "views.py"
            urls = app / "urls.py"
            views.write_text(
                "from django.http import HttpResponseForbidden\n"
                "from rest_framework.views import APIView\n\n"
                "def resolve_company(request):\n"
                "    return object()\n\n"
                "class PublicOrderView(APIView):\n"
                "    permission_classes = []\n"
                "    def post(self, request):\n"
                "        company = resolve_company(request)\n"
                "        if not company:\n"
                "            return HttpResponseForbidden()\n"
                "        return None\n",
                encoding="utf-8",
            )
            urls.write_text(
                "from django.urls import path\n"
                "from .views import PublicOrderView\n\n"
                "urlpatterns = [path('public/orders/', PublicOrderView.as_view())]\n",
                encoding="utf-8",
            )

            build = _build(root, (views, urls))
            _assert_support_facts_reference_entities(build)
            kg = _snapshot(root, build)

        result = call_tool(kg, "get_service_brief", {"service": "orders", "limit": 10})
        endpoint = result["authz_surface"]["endpoint_authorization"][0]
        self.assertEqual(endpoint["checks"][0]["qualifier"]["source_kind"], "custom_guard_call")

    def test_authz_surface_does_not_promote_custom_guard_without_auth_failure_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            views = app / "views.py"
            urls = app / "urls.py"
            views.write_text(
                "from rest_framework import status\n"
                "from rest_framework.response import Response\n"
                "from rest_framework.views import APIView\n\n"
                "def resolve_company(request):\n"
                "    return object()\n\n"
                "class PublicOrderView(APIView):\n"
                "    permission_classes = []\n"
                "    def post(self, request):\n"
                "        company = resolve_company(request)\n"
                "        if not company:\n"
                "            return Response({'error': 'Bad request'}, status=status.HTTP_400_BAD_REQUEST)\n"
                "        return None\n",
                encoding="utf-8",
            )
            urls.write_text(
                "from django.urls import path\n"
                "from .views import PublicOrderView\n\n"
                "urlpatterns = [path('public/orders/', PublicOrderView.as_view())]\n",
                encoding="utf-8",
            )

            build = _build(root, (views, urls))
            _assert_support_facts_reference_entities(build)
            kg = _snapshot(root, build)

        result = call_tool(kg, "get_service_brief", {"service": "orders", "limit": 10})
        endpoint = result["authz_surface"]["endpoint_authorization"][0]
        self.assertEqual(endpoint["authz_status"], "missing_declared_policy")
        self.assertEqual(endpoint["checks"], [])

    def test_authz_surface_does_not_promote_guard_from_unrelated_nested_failure_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            views = app / "views.py"
            urls = app / "urls.py"
            views.write_text(
                "from rest_framework import status\n"
                "from rest_framework.response import Response\n"
                "from rest_framework.views import APIView\n\n"
                "def resolve_company(request):\n"
                "    return object()\n\n"
                "class PublicOrderView(APIView):\n"
                "    permission_classes = []\n"
                "    def post(self, request):\n"
                "        company = resolve_company(request)\n"
                "        if not company:\n"
                "            if request.data.get('force'):\n"
                "                return Response({'error': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)\n"
                "            return None\n"
                "        return None\n",
                encoding="utf-8",
            )
            urls.write_text(
                "from django.urls import path\n"
                "from .views import PublicOrderView\n\n"
                "urlpatterns = [path('public/orders/', PublicOrderView.as_view())]\n",
                encoding="utf-8",
            )

            build = _build(root, (views, urls))
            _assert_support_facts_reference_entities(build)
            kg = _snapshot(root, build)

        result = call_tool(kg, "get_service_brief", {"service": "orders", "limit": 10})
        endpoint = result["authz_surface"]["endpoint_authorization"][0]
        self.assertEqual(endpoint["authz_status"], "missing_declared_policy")
        self.assertEqual(endpoint["checks"], [])

    def test_authz_surface_extracts_custom_guard_inside_match_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            views = app / "views.py"
            urls = app / "urls.py"
            views.write_text(
                "from http import HTTPStatus\n"
                "from rest_framework import status\n"
                "from rest_framework.response import Response\n"
                "from rest_framework.views import APIView\n\n"
                "def resolve_company(request):\n"
                "    return object()\n\n"
                "class PublicOrderView(APIView):\n"
                "    permission_classes = []\n"
                "    def post(self, request):\n"
                "        match request.method:\n"
                "            case 'POST':\n"
                "                company = resolve_company(request)\n"
                "                if company is None:\n"
                "                    return Response({'error': 'Unauthorized'}, status=HTTPStatus.FORBIDDEN)\n"
                "        return None\n",
                encoding="utf-8",
            )
            urls.write_text(
                "from django.urls import path\n"
                "from .views import PublicOrderView\n\n"
                "urlpatterns = [path('public/orders/', PublicOrderView.as_view())]\n",
                encoding="utf-8",
            )

            build = _build(root, (views, urls))
            _assert_support_facts_reference_entities(build)
            kg = _snapshot(root, build)

        result = call_tool(kg, "get_service_brief", {"service": "orders", "limit": 10})
        endpoint = result["authz_surface"]["endpoint_authorization"][0]
        self.assertEqual(endpoint["authz_status"], "authz_evidence_found")
        self.assertEqual(endpoint["checks"][0]["qualifier"]["source_kind"], "custom_guard_call")

    def test_authz_surface_prioritizes_review_leads_before_section_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            views = app / "views.py"
            urls = app / "urls.py"
            views.write_text(
                "from rest_framework import status\n"
                "from rest_framework.response import Response\n"
                "from rest_framework.views import APIView\n"
                "from orders.api_key_validator import get_company_by_api_key\n\n"
                "class MissingPolicyView(APIView):\n"
                "    def get(self, request):\n"
                "        return None\n\n"
                "class GuardedOrderView(APIView):\n"
                "    permission_classes = []\n"
                "    def post(self, request):\n"
                "        company = get_company_by_api_key(request)\n"
                "        if not company:\n"
                "            return Response({'error': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)\n"
                "        return None\n",
                encoding="utf-8",
            )
            urls.write_text(
                "from django.urls import path\n"
                "from .views import GuardedOrderView, MissingPolicyView\n\n"
                "urlpatterns = [\n"
                "    path('aaa/missing/', MissingPolicyView.as_view()),\n"
                "    path('zzz/guarded/', GuardedOrderView.as_view()),\n"
                "]\n",
                encoding="utf-8",
            )

            build = _build(root, (views, urls))
            _assert_support_facts_reference_entities(build)
            kg = _snapshot(root, build)

        result = call_tool(kg, "get_service_brief", {"service": "orders", "limit": 1})
        authz = result["authz_surface"]
        self.assertEqual(authz["summary"]["section_limit"], 1)
        self.assertEqual(
            authz["summary"]["inspection_ref_count"],
            sum(len(area.get("inspection_refs", [])) for area in authz["inspection_areas"]),
        )
        self.assertEqual(
            authz["summary"]["inspection_row_count"],
            sum(area.get("inspection_row_count", 0) for area in authz["inspection_areas"]),
        )
        self.assertEqual(authz["endpoint_authorization"][0]["endpoint"]["path"], "/aaa/missing/")
        self.assertEqual(authz["review_leads"][0]["lead_type"], "custom_guard")
        self.assertEqual(authz["review_leads"][0]["endpoint"]["path"], "/zzz/guarded/")
        omitted_area = next(area for area in authz["inspection_areas"] if area["area"] == "omitted_endpoint_rows")
        self.assertEqual(omitted_area["inspection_refs"][0]["endpoint"]["path"], "/zzz/guarded/")
        self.assertEqual(omitted_area["inspection_refs"][0]["handler"]["path"], "orders/views.py")
        self.assertEqual(omitted_area["inspection_refs"][0]["handler"]["qualname"], "GuardedOrderView")
        self.assertIn(
            "/zzz/guarded/",
            {ref["endpoint"]["path"] for ref in authz["inspection_index"] if "endpoint" in ref},
        )
        self.assertIn(
            "omitted_endpoint_rows",
            {area["area"] for area in authz["inspection_areas"]},
        )

    def test_authz_surface_keeps_public_and_guard_leads_for_same_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            views = app / "views.py"
            urls = app / "urls.py"
            views.write_text(
                "from rest_framework import status\n"
                "from rest_framework.permissions import AllowAny\n"
                "from rest_framework.response import Response\n"
                "from rest_framework.views import APIView\n"
                "from orders.api_key_validator import get_company_by_api_key\n\n"
                "class PublicWebhookView(APIView):\n"
                "    permission_classes = [AllowAny]\n"
                "    def post(self, request):\n"
                "        company = get_company_by_api_key(request)\n"
                "        if not company:\n"
                "            return Response({'error': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)\n"
                "        return None\n",
                encoding="utf-8",
            )
            urls.write_text(
                "from django.urls import path\n"
                "from .views import PublicWebhookView\n\n"
                "urlpatterns = [path('webhook/', PublicWebhookView.as_view())]\n",
                encoding="utf-8",
            )

            build = _build(root, (views, urls))
            _assert_support_facts_reference_entities(build)
            kg = _snapshot(root, build)

        result = call_tool(kg, "get_service_brief", {"service": "orders", "limit": 10})
        authz = result["authz_surface"]
        lead_types = {lead["lead_type"] for lead in authz["review_leads"]}
        self.assertIn("public_policy_present", lead_types)
        self.assertIn("custom_guard", lead_types)
        endpoint = authz["endpoint_authorization"][0]
        self.assertTrue(endpoint["public_policy_present"])
        self.assertEqual(endpoint["authz_status"], "authz_evidence_found")

    def test_authz_surface_reports_languages_without_authz_extractors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            views = app / "views.py"
            urls = app / "urls.py"
            views.write_text(
                "from rest_framework.views import APIView\n\n"
                "class OrdersView(APIView):\n"
                "    def get(self, request):\n"
                "        return None\n",
                encoding="utf-8",
            )
            urls.write_text(
                "from django.urls import path\n"
                "from .views import OrdersView\n\n"
                "urlpatterns = [path('orders/', OrdersView.as_view())]\n",
                encoding="utf-8",
            )

            build = _build(root, (views, urls))
            _assert_support_facts_reference_entities(build)
            kg = _snapshot(
                root,
                build,
                manifest_counts={
                    "files_by_language": {"elixir": 1, "markdown": 4, "python": 2, "typescript": 3},
                    "unsupported_files_by_language": {"markdown": 2, "ruby": 1},
                },
            )

        result = call_tool(kg, "get_service_brief", {"service": "orders", "limit": 10})
        unsupported_by_language = {
            row["language"]: row["reason"] for row in result["authz_surface"]["unsupported_scopes"]
        }
        self.assertEqual(unsupported_by_language["elixir"], "language_has_no_authz_extractor")
        self.assertEqual(unsupported_by_language["typescript"], "language_has_no_authz_extractor")
        self.assertEqual(unsupported_by_language["ruby"], "unsupported_language")
        self.assertNotIn("markdown", unsupported_by_language)
        unsupported_area = next(
            area for area in result["authz_surface"]["inspection_areas"] if area["area"] == "unsupported_authz_scopes"
        )
        self.assertEqual(
            {ref["language"] for ref in unsupported_area["inspection_refs"]},
            {"elixir", "typescript", "ruby"},
        )

    def test_authz_surface_detects_permission_classes_through_local_base_class(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            views = app / "views.py"
            urls = app / "urls.py"
            views.write_text(
                "from rest_framework.permissions import BasePermission\n"
                "from rest_framework.views import APIView\n\n"
                "class AppBasePermission(BasePermission):\n"
                "    pass\n\n"
                "class StaffOnly(AppBasePermission):\n"
                "    def has_permission(self, request, view):\n"
                "        return request.user.is_staff\n\n"
                "class OrderAdminView(APIView):\n"
                "    permission_classes = [StaffOnly]\n"
                "    def post(self, request):\n"
                "        return None\n",
                encoding="utf-8",
            )
            urls.write_text(
                "from django.urls import path\n"
                "from .views import OrderAdminView\n\n"
                "urlpatterns = [path('orders/admin/', OrderAdminView.as_view())]\n",
                encoding="utf-8",
            )

            build = _build(root, (views, urls))
            _assert_support_facts_reference_entities(build)
            kg = _snapshot(root, build)

        result = call_tool(kg, "get_service_brief", {"service": "orders", "limit": 10})
        endpoint = result["authz_surface"]["endpoint_authorization"][0]
        policy_names = {row["qualifier"]["policy"] for row in endpoint["policies"]}
        self.assertIn("StaffOnly", policy_names)
        declared_policy_names = {
            row["qualifier"]["policy"] for row in result["authz_surface"]["declared_policies"]
        }
        self.assertIn("StaffOnly", declared_policy_names)

    def test_authz_surface_joins_method_decorator_policy_to_class_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            views = app / "views.py"
            urls = app / "urls.py"
            views.write_text(
                "from django.contrib.auth.decorators import login_required\n"
                "from rest_framework.views import APIView\n\n"
                "class SecureView(APIView):\n"
                "    @login_required\n"
                "    def get(self, request):\n"
                "        return None\n",
                encoding="utf-8",
            )
            urls.write_text(
                "from django.urls import path\n"
                "from .views import SecureView\n\n"
                "urlpatterns = [path('secure/', SecureView.as_view())]\n",
                encoding="utf-8",
            )

            build = _build(root, (views, urls))
            _assert_support_facts_reference_entities(build)
            kg = _snapshot(root, build)

        result = call_tool(kg, "get_service_brief", {"service": "orders", "limit": 10})
        endpoint = result["authz_surface"]["endpoint_authorization"][0]
        self.assertEqual(endpoint["endpoint"]["path"], "/secure/")
        self.assertEqual(endpoint["authz_status"], "authz_evidence_found")
        self.assertEqual(endpoint["policies"][0]["subject"]["qualname"], "SecureView.get")
        self.assertEqual(endpoint["policies"][0]["qualifier"]["policy"], "login_required")

    def test_authz_surface_detects_drf_self_check_on_local_base_view_subclass(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            views = app / "views.py"
            urls = app / "urls.py"
            views.write_text(
                "from rest_framework.views import APIView\n\n"
                "class BaseOrderView(APIView):\n"
                "    pass\n\n"
                "class SecureOrderView(BaseOrderView):\n"
                "    def post(self, request):\n"
                "        self.check_permissions(request)\n"
                "        return None\n",
                encoding="utf-8",
            )
            urls.write_text(
                "from django.urls import path\n"
                "from .views import SecureOrderView\n\n"
                "urlpatterns = [path('secure/orders/', SecureOrderView.as_view())]\n",
                encoding="utf-8",
            )

            build = _build(root, (views, urls))
            _assert_support_facts_reference_entities(build)
            kg = _snapshot(root, build)

        result = call_tool(kg, "get_service_brief", {"service": "orders", "limit": 10})
        endpoint = result["authz_surface"]["endpoint_authorization"][0]
        self.assertEqual(endpoint["checks"][0]["qualifier"]["check"], "check_permissions")

    def test_flask_authz_surface_uses_route_decorator_and_auth_decorator(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            flask_app = app / "app.py"
            flask_app.write_text(
                "from flask import Flask\n"
                "from flask_jwt_extended import jwt_required\n"
                "from flask_login import current_user\n\n"
                "app = Flask(__name__)\n\n"
                "@app.route('/admin', methods=['POST'])\n"
                "@jwt_required()\n"
                "def admin_action():\n"
                "    if current_user.is_authenticated:\n"
                "        return None\n"
                "    return None\n",
                encoding="utf-8",
            )

            build = _build(root, (flask_app,))
            _assert_support_facts_reference_entities(build)
            kg = _snapshot(root, build)

        result = call_tool(kg, "get_service_brief", {"service": "orders", "limit": 10})
        endpoint = result["authz_surface"]["endpoint_authorization"][0]
        self.assertEqual(endpoint["endpoint"]["path"], "/admin")
        self.assertEqual(endpoint["route"]["method"], "POST")
        self.assertEqual(endpoint["handler"]["qualname"], "admin_action")
        self.assertEqual(endpoint["authz_status"], "authz_evidence_found")
        self.assertEqual(endpoint["policies"][0]["qualifier"]["policy"], "jwt_required")
        self.assertEqual(endpoint["checks"][0]["qualifier"]["check"], "is_authenticated")

    def test_flask_authz_surface_extracts_custom_guard_with_abort_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "orders"
            app.mkdir()
            flask_app = app / "app.py"
            flask_app.write_text(
                "from flask import Flask, abort\n\n"
                "app = Flask(__name__)\n\n"
                "def verify_api_key(request):\n"
                "    return None\n\n"
                "@app.route('/admin', methods=['POST'])\n"
                "def admin_action():\n"
                "    if not verify_api_key(None):\n"
                "        return abort(401)\n"
                "    return None\n",
                encoding="utf-8",
            )

            build = _build(root, (flask_app,))
            _assert_support_facts_reference_entities(build)
            kg = _snapshot(root, build)

        result = call_tool(kg, "get_service_brief", {"service": "orders", "limit": 10})
        endpoint = result["authz_surface"]["endpoint_authorization"][0]
        self.assertEqual(endpoint["checks"][0]["qualifier"]["source_kind"], "custom_guard_call")


def _build(root: Path, files: tuple[Path, ...]) -> KgBuild:
    return PythonAstExtractor(include_transport=False).extract(
        RepoSnapshot(
            root=root,
            name="orders",
            owner="test",
            commit_sha="sha",
            files_by_language={"python": files, "typescript": ()},
        )
    )


def _assert_support_facts_reference_entities(build: KgBuild) -> None:
    entity_ids = {entity.entity_id for entity in build.entities}
    for fact in build.support_facts:
        if fact.object_id:
            if fact.object_id not in entity_ids:
                raise AssertionError(f"missing support fact object entity: {fact}")
        if fact.subject_id not in entity_ids:
            raise AssertionError(f"missing support fact subject entity: {fact}")


def _snapshot(root: Path, build: KgBuild, *, manifest_counts: dict[str, object] | None = None) -> KgSnapshot:
    snapshot_dir = root / "snapshot"
    counts: dict[str, object] = {"entities": len(build.entities), "facts": len(build.facts)}
    if manifest_counts:
        counts.update(manifest_counts)
    JsonlKgStore(snapshot_dir).write(
        entities=build.entities,
        facts=build.facts,
        support_facts=build.support_facts,
        evidence=build.evidence,
        coverage=build.coverage,
        manifest={"counts": counts},
    )
    return KgSnapshot(snapshot_dir)
