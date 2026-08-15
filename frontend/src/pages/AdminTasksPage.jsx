import { useEffect, useMemo, useRef, useState } from 'react'
import StatusLamp from '../components/StatusLamp'
import { Link } from 'react-router-dom'
import {
  authLogin,
  authMe,
  fetchTaskLogs,
  fetchTasksOverview,
  startBackgroundTask,
  stopBackgroundTask,
} from '../api'
import AdminLoginPanel from '../components/AdminLoginPanel'

const ADMIN_AUTH_FLAG_KEY = 'admin-authenticated'
const ADMIN_AUTH_USERNAME_KEY = 'admin-username'

function getCachedAdminAuth() {
  if (typeof window === 'undefined') return { authenticated: false, username: null }
  const authenticated = window.sessionStorage.getItem(ADMIN_AUTH_FLAG_KEY) === '1'
  const username = window.sessionStorage.getItem(ADMIN_AUTH_USERNAME_KEY)
  return { authenticated, username }
}

function formatRuntime(seconds) {
  if (!Number.isFinite(seconds)) return 'N/A'
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const secs = seconds % 60
  if (hours > 0) return `${hours}ч ${minutes}м ${secs}с`
  if (minutes > 0) return `${minutes}м ${secs}с`
  return `${secs}с`
}

function AdminTasksPage() {
  const cachedAuth = getCachedAdminAuth()
  const [authState, setAuthState] = useState({
    loading: true,
    authenticated: cachedAuth.authenticated,
    is_staff: cachedAuth.authenticated,
    username: cachedAuth.username,
  })
  const [loginForm, setLoginForm] = useState({ username: '', password: '' })
  const [loginError, setLoginError] = useState('')

  const [overview, setOverview] = useState({ collector: null, running_tasks: [], manual_tasks: [], log_sources: ['app'] })
  const [overviewLoading, setOverviewLoading] = useState(true)
  // null — статус ещё не получен, иначе: 'success', 'timeout', 'error'
  const [overviewStatus, setOverviewStatus] = useState(null)
  const [hasOverviewLoaded, setHasOverviewLoaded] = useState(false)
  const [selectedTask, setSelectedTask] = useState('')
  const [logSource, setLogSource] = useState('app')
  const [logs, setLogs] = useState([])
  const [actionStatus, setActionStatus] = useState('')

  const isAdmin = authState.authenticated && authState.is_staff
  const hasOverviewLoadedRef = useRef(false)
  // Для лампочки статуса: null (ещё не было ответа) — серый, иначе последний статус
  const displayedOverviewStatus = overviewStatus ?? 'none'
  const collector = overview.collector
  const collectorProgress = useMemo(() => {
    if (!collector || !collector.progress_total) return null
    return `${collector.progress_done}/${collector.progress_total}`
  }, [collector])

  useEffect(() => {
    let isActive = true

    async function run() {
      try {
        const me = await authMe()
        if (!isActive) return
        setAuthState({ loading: false, ...me })
        if (me?.authenticated && me?.is_staff) {
          window.sessionStorage.setItem(ADMIN_AUTH_FLAG_KEY, '1')
          if (me?.username) window.sessionStorage.setItem(ADMIN_AUTH_USERNAME_KEY, me.username)
        } else {
          window.sessionStorage.removeItem(ADMIN_AUTH_FLAG_KEY)
          window.sessionStorage.removeItem(ADMIN_AUTH_USERNAME_KEY)
        }
      } catch {
        if (!isActive) return
        if (cachedAuth.authenticated) {
          setAuthState({
            loading: false,
            authenticated: true,
            is_staff: true,
            username: cachedAuth.username,
          })
          return
        }
        setAuthState({ loading: false, authenticated: false, is_staff: false, username: null })
      }
    }

    run()
    return () => {
      isActive = false
    }
  }, [])

  useEffect(() => {
    if (!isAdmin) return undefined

    let isActive = true
    let timerId

    const fetchOverview = async () => {
      try {
        setOverviewLoading(true)
        const data = await fetchTasksOverview()
        if (!isActive) return
        setOverview(data)
        setOverviewStatus('success')
        setHasOverviewLoaded(true)
        hasOverviewLoadedRef.current = true
        setOverviewLoading(false)
        if (data.manual_tasks?.length) {
          setSelectedTask((prev) => prev || data.manual_tasks[0])
        }
      } catch (error) {
        if (!isActive) return
        const message = error.message || 'Не удалось получить статус задач'
        setOverviewStatus(message.toLowerCase().includes('timeout') ? 'timeout' : 'error')
        setOverviewLoading(false)
        if (message.toLowerCase().includes('timeout')) {
          console.error('[AdminTasksPage] Request timeout while loading tasks overview')
        } else {
          console.error('[AdminTasksPage] Failed to load tasks overview:', message)
        }
      } finally {
        if (!isActive) return
        timerId = setTimeout(fetchOverview, 4000)
      }
    }

    fetchOverview()

    return () => {
      isActive = false
      clearTimeout(timerId)
    }
  }, [isAdmin])

  useEffect(() => {
    if (!isAdmin || !hasOverviewLoaded) return undefined

    let isActive = true
    let timerId

    const fetchLogs = async () => {
      try {
        const data = await fetchTaskLogs(logSource, 80)
        if (!isActive) return
        setLogs(data.lines || [])
      } catch {
        if (!isActive) return
      } finally {
        if (!isActive) return
        timerId = setTimeout(fetchLogs, 8000)
      }
    }

    fetchLogs()

    return () => {
      isActive = false
      clearTimeout(timerId)
    }
  }, [isAdmin, hasOverviewLoaded, logSource])

  const handleLogin = async (event) => {
    event.preventDefault()
    setLoginError('')

    try {
      const result = await authLogin(loginForm.username, loginForm.password)
      setAuthState({ loading: false, authenticated: true, ...result })
      window.sessionStorage.setItem(ADMIN_AUTH_FLAG_KEY, '1')
      window.sessionStorage.setItem(ADMIN_AUTH_USERNAME_KEY, result.username || loginForm.username)
      window.dispatchEvent(new CustomEvent('admin-auth-changed', { detail: { authenticated: true } }))
      setLoginForm({ username: '', password: '' })
    } catch (error) {
      setLoginError(error.message || 'Ошибка входа')
    }
  }

  const handleStartTask = async () => {
    if (!selectedTask) return
    try {
      const result = await startBackgroundTask(selectedTask)
      setActionStatus(result.already_running
        ? `Задача ${result.task_name} уже выполняется (${result.task_id})`
        : `Запущена задача ${result.task_name} (${result.task_id})`)
    } catch (error) {
      console.error('[AdminTasksPage] Failed to start task:', error.message || 'Не удалось запустить задачу')
    }
  }

  const handleStopTask = async (taskId) => {
    try {
      const result = await stopBackgroundTask(taskId)
      setActionStatus(`Запрошена остановка задачи ${result.task_name} — завершится на ближайшей контрольной точке`)
    } catch (error) {
      console.error('[AdminTasksPage] Failed to stop task:', error.message || 'Не удалось остановить задачу')
    }
  }

  const handleCollectorStart = async () => {
    if (!overview.collector_task_name) return
    try {
      const result = await startBackgroundTask(overview.collector_task_name)
      setActionStatus(result.already_running ? 'Сборщик уже работает' : 'Сборщик истории запущен')
    } catch (error) {
      console.error('[AdminTasksPage] Failed to start collector:', error.message || 'Не удалось запустить сборщик')
    }
  }

  const handleCollectorStop = async () => {
    if (!collector?.task?.id) return
    await handleStopTask(collector.task.id)
  }

  if (authState.loading) {
    return <div className="text-secondary">Проверка доступа...</div>
  }

  if (!isAdmin) {
    return (
      <AdminLoginPanel
        accessTargetText="[M0ДYЛЬ_З4Д4Ч]"
        submitCipherText="X1-7A:П0ДТВ3РДИТЬ"
        username={loginForm.username}
        password={loginForm.password}
        loginError={loginError}
        onSubmit={handleLogin}
        onUsernameChange={(value) => setLoginForm((prev) => ({ ...prev, username: value }))}
        onPasswordChange={(value) => setLoginForm((prev) => ({ ...prev, password: value }))}
      />
    )
  }

  return (
    <div className="d-flex flex-column gap-3">
      <div className="glass-panel p-3">
        <div className="small text-secondary mb-2">
          <Link to="/control-center" className="text-secondary text-decoration-none">Центр управления</Link>
          {' < Фоновые задачи'}
        </div>
        <h4 className="mb-0">Фоновые задачи</h4>
      </div>

      <div className="row g-3">
        <div className="col-12 col-xl-4">
          <div className="glass-panel p-3 h-100">
            <h6 className="panel-title mb-3">Управление задачами</h6>

            <label className="small text-secondary mb-1">Запуск задачи</label>
            <div className="d-flex gap-2">
              <select className="form-select" value={selectedTask} onChange={(event) => setSelectedTask(event.target.value)}>
                {(overview.manual_tasks || []).length > 0 ? (
                  (overview.manual_tasks || []).map((taskName) => (
                    <option key={taskName} value={taskName}>{taskName}</option>
                  ))
                ) : (
                  <option value="" disabled>Нет доступных задач</option>
                )}
              </select>
              <button
                type="button"
                className="btn btn-accent"
                onClick={handleStartTask}
                disabled={!selectedTask || (overview.manual_tasks || []).length === 0}
              >
                Start
              </button>
            </div>

            <div className="mt-3 d-flex justify-content-between align-items-start gap-2">
              <div>
                <div className="small text-secondary">Running: {overview.running_tasks?.length || 0}</div>
              </div>
              <div className="d-flex align-items-center gap-2">
                <StatusLamp type="status" value={displayedOverviewStatus} />
                <StatusLamp type="loading" value={overviewLoading ? 'loading' : 'idle'} />
              </div>
            </div>

            {actionStatus && <div className="alert alert-info mt-3 mb-0 py-2">{actionStatus}</div>}
          </div>
        </div>

        <div className="col-12 col-xl-8">
          <div className="glass-panel p-3 h-100">
            <div className="d-flex justify-content-between align-items-center flex-wrap gap-2 mb-3">
              <h6 className="panel-title mb-0">Сборщик истории аукциона</h6>
              <div className="d-flex gap-2">
                <button
                  type="button"
                  className="btn btn-sm btn-accent"
                  onClick={handleCollectorStart}
                  disabled={collector?.alive}
                >
                  Запустить
                </button>
                <button
                  type="button"
                  className="btn btn-sm btn-outline-danger"
                  onClick={handleCollectorStop}
                  disabled={!collector?.alive}
                >
                  Остановить
                </button>
              </div>
            </div>

            <div className="small text-secondary">
              Статус: {(() => {
                if (!collector?.alive) return 'остановлен'
                if (collector?.state === 'waiting_lock') return 'standby (блокировку держит другой инстанс)'
                if (collector?.state === 'lock_error') return 'ошибка блокировки (см. последнюю ошибку ниже)'
                return 'работает'
              })()}
            </div>
            {collectorProgress && (
              <div className="small text-secondary">Прогресс цикла: {collectorProgress}</div>
            )}
            <div className="small text-secondary">Циклов завершено: {collector?.cycles_completed ?? 0}</div>
            {collector?.task && (
              <div className="small text-secondary">Работает: {formatRuntime(collector.task.runtime_seconds)}</div>
            )}
            {collector?.last_cycle_finished_at && (
              <div className="small text-secondary">Последний цикл завершен: {collector.last_cycle_finished_at}</div>
            )}
            {collector?.last_error && (
              <div className="alert alert-warning mt-2 mb-0 py-2">Последняя ошибка: {collector.last_error}</div>
            )}
          </div>
        </div>
      </div>

      <div className="glass-panel p-3">
        <h6 className="panel-title mb-3">Активные задачи</h6>

        {overview.running_tasks?.length ? (
          <div className="d-flex flex-column gap-2">
            {overview.running_tasks.map((task) => (
              <div key={task.id} className="admin-task-row">
                <div>
                  <div className="fw-semibold">{task.name}</div>
                  <div className="small text-secondary">{task.id}</div>
                  <div className="small text-secondary">Работает: {formatRuntime(task.runtime_seconds)}</div>
                  {task.stop_requested && <div className="small text-warning">Останавливается...</div>}
                </div>
                {task.stoppable && (
                  <button
                    type="button"
                    className="btn btn-sm btn-outline-danger"
                    disabled={task.stop_requested}
                    onClick={() => handleStopTask(task.id)}
                  >
                    Stop
                  </button>
                )}
              </div>
            ))}
          </div>
        ) : (
          <div className="text-secondary">Сейчас нет активных задач.</div>
        )}
      </div>

      <div className="glass-panel p-3">
        <div className="d-flex justify-content-between align-items-center flex-wrap gap-2 mb-3">
          <h6 className="panel-title mb-0">Логи в реальном времени</h6>
          <select className="form-select admin-log-source" value={logSource} onChange={(event) => setLogSource(event.target.value)}>
            {(overview.log_sources || ['app']).map((source) => (
              <option key={source} value={source}>Task logs ({source})</option>
            ))}
          </select>
        </div>
        <pre className="admin-log-box mb-0">{logs.join('\n') || 'Логи пока пусты'}</pre>
      </div>
    </div>
  )
}

export default AdminTasksPage
