import { TelegramWebApp } from '../types';

export function getTelegram(): TelegramWebApp | null {
  return window.Telegram?.WebApp || null;
}

export function getInitData(): string {
  return getTelegram()?.initData || '';
}

export function getUser() {
  return getTelegram()?.initDataUnsafe?.user || null;
}

export function getStartParam(): string {
  return getTelegram()?.initDataUnsafe?.start_param || '';
}

export function hapticTap(style: 'light' | 'medium' | 'heavy' = 'light') {
  try {
    getTelegram()?.HapticFeedback?.impactOccurred(style);
  } catch {}
}

export function hapticSuccess() {
  try {
    getTelegram()?.HapticFeedback?.notificationOccurred('success');
  } catch {}
}

export function hapticError() {
  try {
    getTelegram()?.HapticFeedback?.notificationOccurred('error');
  } catch {}
}
