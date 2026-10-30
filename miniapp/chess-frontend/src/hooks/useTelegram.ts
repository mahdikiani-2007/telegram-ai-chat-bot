import { useEffect, useState } from 'react';
import { getInitData, getStartParam, getTelegram, getUser } from '../services/telegram';
import { TelegramWebApp } from '../types';

export function useTelegram() {
  const [tg] = useState<TelegramWebApp | null>(getTelegram);
  const [user] = useState(getUser);
  const [initData] = useState(getInitData);
  const [startParam] = useState(getStartParam);

  useEffect(() => {
    if (tg) {
      try {
        tg.ready();
        tg.expand();
        tg.setHeaderColor('#070a12');
        tg.setBackgroundColor('#070a12');
        if (tg.disableVerticalSwipes) {
          tg.disableVerticalSwipes();
        }
      } catch {}
    }
  }, [tg]);

  return { tg, user, initData, startParam };
}
