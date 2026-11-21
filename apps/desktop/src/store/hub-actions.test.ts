import { beforeEach, describe, expect, it, vi } from 'vitest'

// A failed hub action (non-zero subprocess exit) must REJECT so callers toast
// it — the Aug 2026 report shape was a scan-gated/failed `hermes skills
// install` exiting non-zero with no toast, no list change, and no error
// anywhere in the UI: the user read it as "install did nothing".

const getActionStatus = vi.fn()
const installSkillFromHub = vi.fn()
const confirm = vi.fn()

vi.mock('@/hermes', () => ({
  getActionStatus: (...args: unknown[]) => getActionStatus(...args),
  installSkillFromHub: (...args: unknown[]) => installSkillFromHub(...args),
  uninstallSkillFromHub: vi.fn(),
  updateSkillsFromHub: vi.fn()
}))

vi.mock('@/lib/query-client', () => ({
  queryClient: { invalidateQueries: vi.fn() }
}))

vi.mock('@/store/confirm', () => ({
  confirm: (...args: unknown[]) => confirm(...args)
}))

vi.mock('@/lib/slash-completion-cache', () => ({
  invalidateSlashCompletions: vi.fn()
}))

vi.mock('@/store/activity', () => ({
  upsertDesktopActionTask: vi.fn()
}))

// hub-actions subscribes to the active gateway profile at module scope (its
// per-profile state wipe); a minimal stub keeps this test off the real store.
vi.mock('@/store/profile', () => ({
  $activeGatewayProfile: { subscribe: vi.fn() },
  normalizeProfileKey: (value: unknown) => String(value ?? '') || 'default'
}))

import { $hubActions, $hubInstalledOverride, installHubSkill } from './hub-actions'

describe('installHubSkill failure surfacing', () => {
  beforeEach(() => {
    getActionStatus.mockReset()
    installSkillFromHub.mockReset()
    confirm.mockReset()
    $hubActions.set({})
    $hubInstalledOverride.set({})
  })

  it('rejects with the action log tail when the subprocess exits non-zero', async () => {
    installSkillFromHub.mockResolvedValue({ name: 'skills-install-ascii-art-dd7bccf1' })
    getActionStatus.mockResolvedValue({
      name: 'skills-install-ascii-art-dd7bccf1',
      running: false,
      exit_code: 1,
      pid: 4242,
      lines: ['Resolving ascii-art...', 'Error: security scan blocked install']
    })

    await expect(installHubSkill('ascii-art')).rejects.toThrow('security scan blocked install')

    // A failed install must not paint the row as installed.
    expect($hubInstalledOverride.get()['ascii-art']).toBeUndefined()
    // ...but the row's running flag still settles to false for the button.
    expect($hubActions.get()['ascii-art']?.running).toBe(false)
  })

  it('rejects with the exit code when the log carries no detail', async () => {
    installSkillFromHub.mockResolvedValue({ name: 'skills-install-x-00000000' })
    getActionStatus.mockResolvedValue({
      name: 'skills-install-x-00000000',
      running: false,
      exit_code: 7,
      pid: 4242,
      lines: []
    })

    await expect(installHubSkill('x')).rejects.toThrow('exited with code 7')
  })

  it('resolves and flips the row on a clean exit', async () => {
    installSkillFromHub.mockResolvedValue({ name: 'skills-install-ok-11111111' })
    getActionStatus.mockResolvedValue({
      name: 'skills-install-ok-11111111',
      running: false,
      exit_code: 0,
      pid: 4242,
      lines: ['Installed ok']
    })

    await expect(installHubSkill('ok')).resolves.toBeUndefined()
    expect($hubInstalledOverride.get()['ok']).toBe(true)
  })

  it('asks before env inclusion and retries with the exact one-time token', async () => {
    confirm.mockResolvedValue(true)
    installSkillFromHub
      .mockResolvedValueOnce({
        status: 'confirmation_required',
        name: 'demo-skill',
        environment_files: ['.env.sample'],
        environment_confirmation_token: 'approval-token',
        message: 'Choose Include or Exclude'
      })
      .mockResolvedValueOnce({ name: 'skills-install-demo-skill-22222222' })
    getActionStatus.mockResolvedValue({
      name: 'skills-install-demo-skill-22222222',
      running: false,
      exit_code: 0,
      pid: 4242,
      lines: ['Installed demo-skill']
    })

    await expect(installHubSkill('demo-skill')).resolves.toBeUndefined()

    expect(confirm).toHaveBeenCalledWith(expect.objectContaining({
      title: 'Environment files detected',
      confirmLabel: 'Include files',
      cancelLabel: 'Exclude and install rest',
      details: [{ label: 'File 1', value: '.env.sample' }]
    }))
    expect(installSkillFromHub).toHaveBeenNthCalledWith(2, 'demo-skill', undefined, {
      includeEnvironmentFiles: true,
      environmentConfirmationToken: 'approval-token'
    })
    expect($hubInstalledOverride.get()['demo-skill']).toBe(true)
  })
})
