import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Dialog } from "./Dialog";
import { useLocale } from "../../app/locale";

export interface ConfirmOptions {
  title: string;
  description: string;
  confirmLabel?: string;
  destructive?: boolean;
}
type Confirmation = (options: ConfirmOptions) => Promise<boolean>;
const ConfirmContext = createContext<Confirmation>(async () => false);

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const { t } = useLocale();
  const [options, setOptions] = useState<ConfirmOptions | null>(null);
  const pending = useRef<((answer: boolean) => void) | null>(null);
  const confirm = useCallback<Confirmation>((next) => {
    pending.current?.(false);
    setOptions(next);
    return new Promise((resolve) => {
      pending.current = resolve;
    });
  }, []);
  const resolve = (answer: boolean) => {
    pending.current?.(answer);
    pending.current = null;
    setOptions(null);
  };
  useEffect(() => () => pending.current?.(false), []);
  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      <Dialog
        open={!!options}
        onOpenChange={(open) => {
          if (!open) resolve(false);
        }}
        title={options?.title ?? ""}
        description={options?.description}
      >
        <div className="confirm-actions">
          <button className="button" autoFocus onClick={() => resolve(false)}>
            {t("取消", "Cancel")}
          </button>
          <button
            className={`button ${options?.destructive ? "danger-button" : "primary"}`}
            onClick={() => resolve(true)}
          >
            {options?.confirmLabel ?? t("确认", "Confirm")}
          </button>
        </div>
      </Dialog>
    </ConfirmContext.Provider>
  );
}

export function useConfirm() {
  return useContext(ConfirmContext);
}
