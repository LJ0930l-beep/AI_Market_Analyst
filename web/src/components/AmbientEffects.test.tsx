import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider, LANGUAGE_STORAGE_KEY } from '../i18n';
import { AmbientEffects, AmbientEffectsProvider, AmbientMotionToggle } from './AmbientEffects';

let matchMediaDescriptor: PropertyDescriptor | undefined;

function renderAmbientEffects() {
  return render(
    <I18nProvider>
      <AmbientEffectsProvider>
        <AmbientEffects />
        <AmbientMotionToggle />
      </AmbientEffectsProvider>
    </I18nProvider>,
  );
}

describe('AmbientEffects', () => {
  beforeEach(() => {
    localStorage.clear();
    matchMediaDescriptor = Object.getOwnPropertyDescriptor(window, 'matchMedia');
    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    if (matchMediaDescriptor) Object.defineProperty(window, 'matchMedia', matchMediaDescriptor);
    else Reflect.deleteProperty(window, 'matchMedia');
    localStorage.clear();
    delete document.documentElement.dataset.motion;
  });

  it('localizes the toolbar toggle and persists its setting', () => {
    localStorage.setItem(LANGUAGE_STORAGE_KEY, 'en');
    renderAmbientEffects();

    const toggle = screen.getByRole('button', { name: 'Ambient motion toggle' });
    expect(toggle).toHaveAttribute('aria-pressed', 'true');
    expect(toggle).toHaveTextContent('Ambient motion On');

    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute('aria-pressed', 'false');
    expect(localStorage.getItem('aima-motion')).toBe('off');
    expect(document.documentElement.dataset.motion).toBe('off');
    expect(document.querySelector('.ambient-root')).toHaveAttribute('data-visible', 'false');
  });

  it('respects reduced motion by default and pauses effects while the tab is hidden', () => {
    const mediaQuery = {
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
    };
    Object.defineProperty(window, 'matchMedia', { configurable: true, value: vi.fn(() => mediaQuery) });
    localStorage.setItem(LANGUAGE_STORAGE_KEY, 'zh-CN');
    renderAmbientEffects();

    const toggle = screen.getByRole('button', { name: '环境动效开关' });
    expect(toggle).toHaveAttribute('aria-pressed', 'false');
    expect(document.documentElement.dataset.motion).toBe('off');

    fireEvent.click(toggle);
    expect(document.querySelector('.ambient-root')).toHaveAttribute('data-visible', 'true');

    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    fireEvent(document, new Event('visibilitychange'));
    expect(document.querySelector('.ambient-root')).toHaveAttribute('data-visible', 'false');
  });
});
