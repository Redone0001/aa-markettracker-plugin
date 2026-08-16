from django.shortcuts import redirect
from django.urls import path

from markettracker import views

from .tasks import refresh_contracts
from .views import ItemPriceDetailView

app_name = "markettracker"

urlpatterns = [
    # Main views
    path("manage/", views.manage_stock_view, name="manage_stock"),
    path("list/", views.list_items_view, name="list_items"),
    path("deliveries/", views.deliveries_list_view, name="deliveries_list"),
    path("deliveries/admin/", views.admin_deliveries_view, name="admin_deliveries"),
    path("contracts/errors/", views.contract_errors_view, name="contract_errors"),
    path("item/<int:type_id>/", ItemPriceDetailView.as_view(), name="item_detail"),


    # Character login views
    path("login/manage/", views.character_login_manage, name="character_login_manage"),
    path("login/list/", views.character_login_list, name="character_login_list"),

    # Actions
    path("refresh/", views.refresh_market_data, name="refresh_market_data"),
    path("delete/<int:pk>/", views.delete_trackeditem, name="delete_trackeditem"),
    path("deliveries/create/<int:item_id>/", views.create_delivery, name="create_delivery"),
    path("deliveries/<int:pk>/delete/", views.delete_delivery, name="delete_delivery"),
    path("deliveries/<int:pk>/finish/", views.finish_delivery, name="finish_delivery"),
    path("deliveries/contracts/create/<int:tc_id>/", views.create_contract_delivery, name="create_contract_delivery"),
    path("deliveries/contract/create/<int:tc_id>/",views.create_contract_delivery, name="create_contract_delivery"),
    path("deliveries/contract/<int:pk>/delete/",views.delete_contract_delivery, name="delete_contract_delivery"),
    path("deliveries/contract/<int:pk>/finish/",views.finish_contract_delivery, name="finish_contract_delivery"),
    path("contracts/tracked/<int:pk>/delete/", views.tracked_contract_delete, name="tracked_contract_delete"),
    path("contracts/tracked/<int:pk>/edit/", views.tracked_contract_edit, name="tracked_contract_edit"),
    path("api/item-search/", views.item_search, name="item_search"),
    path("api/fitting-search/", views.fitting_search, name="fitting_search"),


        # Contracts - testowy panel
    path("contracts/", views.contracts_list_view, name="contracts_list"),
    path("contracts/refresh/", lambda r: (refresh_contracts.delay(), redirect("markettracker:contracts_list"))[1], name="refresh_contracts"),

    path("diagnostics/", views.diagnostics_view, name="diagnostics"),

]
