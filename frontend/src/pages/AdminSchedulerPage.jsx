import { useEffect, useRef, useState } from 'react'
import StatusLamp from '../components/StatusLamp'
import { Link } from 'react-router-dom'
import { authLogin, authMe, fetchTasksOverview, startBackgroundTask } from '../api'
import AdminLoginPanel from '../components/AdminLoginPanel'

const ADMIN_AUTH_FLAG_KEY = 'admin-authenticated'
const ADMIN_AUTH_USERNAME_KEY = 'admin-username'

function getCachedAdminAuth() {
  if (typeof window === 'undefined') return { authenticated: false, username: null }
  const authenticated = window.sessionStorage.getItem(ADMIN_AUTH_FLAG_KEY) === '1'
  const username = window.sessionStorage.getItem(ADMIN_AUTH_USERNAME_KEY)
  return { authenticated, username }
}

function AdminSchedulerPage() {
  const cachedAuth = getCachedAdminAuth()
  const [authState, setAuthState] = useState({
    loading: true,
    authenticated: cachedAuth.authenticated,
    is_staff: cachedAuth.authenticated,
    username: cachedAuth.username,
  })
  const [loginForm, setLoginForm] = useState({ username: '', password: '' })
  const [loginError, setLoginError] = useState('')

  const [overview, setOverview] = useState({ schedule: [], running_tasks: [], cron_url_template: '' })
  const [overviewLoading, setOverviewLoading] = useState(true)
  // null — статус ещё не получен, иначе: 'success', 'timeout', 'error'
  const [overviewStatus, setOverviewStatus] = useState(null)
  const [actionStatus, setActionStatus] = useState('')

  const isAdmin = authState.authenticated && authState.is_staff
  const hasOverviewLoadedRef = useRef(false)
  // Для лампочки статуса: null (ещё не было ответа) — серый, иначе последний статус
  const displayedOverviewStatus = overviewStatus ?? 'none'

  const scheduleEntries = overview.schedule || []

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
        hasOverviewLoadedRef.current = true
        setOverviewLoading(false)
      } catch (error) {
        if (!isActive) return
        const message = error.message || 'Не удалось получить расписание задач'
        setOverviewStatus(message.toLowerCase().includes('timeout') ? 'timeout' : 'error')
        setOverviewLoading(false)
        if (message.toLowerCase().includes('timeout')) {
          console.error('[AdminSchedulerPage] Request timeout while loading scheduler overview')
        } else {
          console.error('[AdminSchedulerPage] Failed to load scheduler overview:', message)
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

  const handleRunNow = async (taskName) => {
    if (!taskName) return
    try {
      const result = await startBackgroundTask(taskName)
      setActionStatus(result.already_running
        ? `Задача ${result.task_name} уже выполняется (${result.task_id})`
        : `Задача ${result.task_name} запущена (${result.task_id})`)
    } catch (error) {
      console.error('[AdminSchedulerPage] Failed to run task now:', error.message || 'Не удалось запустить задачу')
    }
  }

  if (authState.loading) {
    return <div className="text-secondary">Проверка доступа...</div>
  }

  if (!isAdmin) {
    return (
      <AdminLoginPanel
        accessTargetText="[PL4NN3R_З4Д4Ч]"
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
          {' < Планировщик'}
        </div>
        <h4 className="mb-0">Планировщик</h4>
      </div>

      <div className="glass-panel p-3 d-flex justify-content-between align-items-start gap-3 flex-wrap">
        <div>
          <div className="small text-secondary">Периодических задач: {scheduleEntries.length}</div>
          <div className="small text-secondary">Сейчас выполняется: {overview.running_tasks?.length || 0}</div>
        </div>
        <div className="d-flex align-items-center gap-2">
          <StatusLamp type="status" value={displayedOverviewStatus} />
          <StatusLamp type="loading" value={overviewLoading ? 'loading' : 'idle'} />
        </div>
      </div>

      {actionStatus && <div className="alert alert-info mb-0 py-2">{actionStatus}</div>}

      <div className="glass-panel p-3">
        <h6 className="panel-title mb-3">Периодические задачи (внешний cron)</h6>
        <div className="small text-secondary mb-3">
          Расписание выполняет внешний cron-сервис (например, cron-job.org), который дергает
          {' '}<code>{overview.cron_url_template || '/auction/api/cron/<task>/?token=<CRON_SECRET>'}</code>.
          Токен задается переменной окружения <code>CRON_SECRET</code> на сервере.
        </div>
        {scheduleEntries.length ? (
          <div className="d-flex flex-column gap-2">
            {scheduleEntries.map((entry) => (
              <div key={entry.name} className="admin-task-row">
                <div>
                  <div className="fw-semibold">{entry.name}</div>
                  <div className="small text-secondary">task: {entry.task || 'N/A'}</div>
                  <div className="small text-secondary">cron: {entry.cron || 'N/A'}</div>
                  <div className="small text-secondary">{entry.description}</div>
                </div>
                <button
                  type="button"
                  className="btn btn-sm btn-outline-light"
                  onClick={() => handleRunNow(entry.task)}
                  disabled={!entry.task}
                >
                  Запустить сейчас
                </button>
              </div>
            ))}
          </div>
        ) : (
          <div className="text-secondary">Периодические задачи не найдены.</div>
        )}
      </div>
    </div>
  )
}

export default AdminSchedulerPage
