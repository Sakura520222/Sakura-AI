import * as Primitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import type { ReactNode } from "react";
import { useLocale } from "../../app/locale";

export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  children: ReactNode;
}) {
  const { t } = useLocale();
  return (
    <Primitive.Root open={open} onOpenChange={onOpenChange}>
      <Primitive.Portal>
        <Primitive.Overlay className="dialog-overlay" />
        <Primitive.Content className="dialog-content">
          <div className="dialog-heading">
            <Primitive.Title>{title}</Primitive.Title>
            <Primitive.Close
              className="icon-button"
              aria-label={t("关闭", "Close")}
            >
              <X size={20} />
            </Primitive.Close>
          </div>
          <Primitive.Description className={description ? "muted" : "sr-only"}>
            {description ?? title}
          </Primitive.Description>
          {children}
        </Primitive.Content>
      </Primitive.Portal>
    </Primitive.Root>
  );
}
