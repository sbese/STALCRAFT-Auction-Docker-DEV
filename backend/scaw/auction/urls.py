from django.urls import path
from django.views.generic import RedirectView
from . import views


urlpatterns = [
    path('', RedirectView.as_view(pattern_name='api-items', permanent=False)),

    path('api/auth/me/', views.api_auth_me, name='api-auth-me'),
    path('api/auth/login/', views.api_auth_login, name='api-auth-login'),
    path('api/auth/logout/', views.api_auth_logout, name='api-auth-logout'),

    path('api/items/', views.api_items, name='api-items'),
    path('api/items/all/', views.api_items_all, name='api-items-all'),
    path('api/items/suggest/', views.api_item_suggest, name='api-item-suggest'),
    path('api/items/<str:item_id>/', views.api_item_detail, name='api-item-detail'),
    path('api/items/<str:item_id>/sales/', views.api_item_sales, name='api-item-sales'),
    path('api/process-lang/', views.api_process_lang_file, name='api_process_lang'),
    path('api/admin/tasks/overview/', views.api_admin_tasks_overview, name='api-admin-tasks-overview'),
    path('api/admin/tasks/start/', views.api_admin_tasks_start, name='api-admin-tasks-start'),
    path('api/admin/tasks/stop/', views.api_admin_tasks_stop, name='api-admin-tasks-stop'),
    path('api/admin/tasks/logs/', views.api_admin_tasks_logs, name='api-admin-tasks-logs'),
    path('api/cron/<str:task_name>/', views.api_cron_task, name='api-cron-task'),
    path('api/health/', views.api_health, name='api-health'),
    path('api/health/collector/', views.api_health_collector, name='api-health-collector'),

]
