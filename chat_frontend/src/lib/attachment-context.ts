import { createContext, useContext } from "react";

export interface AttachedFile {
  id: string;
  name: string;
  mediaType: string; // "image/png", "application/pdf", "text/plain", etc.
  data: string;      // base64 string (no data-URL prefix)
  size: number;      // bytes
}

interface AttachmentContextValue {
  attachments: AttachedFile[];
  setAttachments: React.Dispatch<React.SetStateAction<AttachedFile[]>>;
  addFiles: (files: File[]) => void;
}

export const AttachmentContext = createContext<AttachmentContextValue>({
  attachments: [],
  setAttachments: () => {},
  addFiles: () => {},
});

export const useAttachments = () => useContext(AttachmentContext);
