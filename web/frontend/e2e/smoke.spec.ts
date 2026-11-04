import { expect, test } from '@playwright/test'

test('app loads the Mock workflow workspace', async ({ page }) => {
  await page.goto('/')

  await expect(page.getByRole('heading', { name: 'Workflow' })).toBeVisible()
  await expect(page.getByText('Mock Agent')).toBeVisible()
  await expect(page.getByLabel('Bearer token')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Create session' })).toBeDisabled()
})
