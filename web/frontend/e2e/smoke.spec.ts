import { expect, test } from '@playwright/test'

test('app loads and renders baseline shell', async ({ page }) => {
  await page.goto('/')

  await expect(page.getByRole('heading', { name: 'Workflow' })).toBeVisible()
  await expect(page.getByText('Mock Agent')).toBeVisible()
  await expect(page.getByText('HUB-000')).toBeVisible()
})
