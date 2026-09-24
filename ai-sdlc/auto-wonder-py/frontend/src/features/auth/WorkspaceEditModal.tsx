import { useEffect } from 'react';
import { Form, Input, Modal, message } from 'antd';
import { ApiError, ErrorCodes } from '@/shared/types/common';
import type { WorkspaceInfo } from '@/shared/types/common';
import { useUpdateWorkspace } from './workspaceLifecycleApi';

const { TextArea } = Input;

interface WorkspaceEditFormValues {
  name: string;
  description?: string;
  background?: string;
}

interface WorkspaceEditModalProps {
  workspace: WorkspaceInfo | null;
  onClose: () => void;
}

export function WorkspaceEditModal({ workspace, onClose }: WorkspaceEditModalProps) {
  const [form] = Form.useForm<WorkspaceEditFormValues>();
  const { mutateAsync, isPending } = useUpdateWorkspace();

  // F1.3 回显: keyed on the id, not on `workspace` identity — a list refetch hands back a new
  // object every time and would otherwise discard whatever the user has typed so far.
  useEffect(() => {
    if (!workspace) return;
    form.setFieldsValue({
      name: workspace.name,
      description: workspace.description ?? '',
      background: workspace.background ?? '',
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspace?.id]);

  const handleSubmit = async () => {
    if (!workspace) return;
    let values: WorkspaceEditFormValues;
    try {
      values = await form.validateFields();
    } catch {
      // antd has already rendered the field-level message; a toast on top would be noise.
      return;
    }
    try {
      await mutateAsync({
        id: workspace.id,
        name: values.name,
        description: values.description ?? null,
        background: values.background ?? null,
        version: workspace.version ?? 0,
      });
      message.success('工作空间已更新');
      onClose();
    } catch (e) {
      if (e instanceof ApiError && e.code === ErrorCodes.ORG_VERSION_CONFLICT) {
        // The list refetch triggered by the failed save is what makes a retry possible with a
        // fresh version, so the hint tells the user to reload rather than to click again.
        message.error('工作空间已被其他人修改，请关闭弹窗后重新编辑');
        return;
      }
      message.error(e instanceof ApiError ? e.message : '保存失败，请稍后重试');
    }
  };

  return (
    <Modal
      title={workspace ? `编辑「${workspace.name}」` : ''}
      open={workspace !== null}
      okText="保存"
      cancelText="取消"
      confirmLoading={isPending}
      onCancel={onClose}
      onOk={handleSubmit}
      destroyOnHidden
    >
      <Form form={form} layout="vertical">
        <Form.Item
          name="name"
          label="工作空间名称"
          rules={[
            { required: true, whitespace: true, message: '工作空间名称不能为空' },
            { max: 128, message: '工作空间名称不能超过 128 个字符' },
          ]}
        >
          <Input placeholder="输入工作空间名称" maxLength={128} />
        </Form.Item>
        <Form.Item
          name="description"
          label="工作空间描述"
          rules={[{ max: 512, message: '工作空间描述不能超过 512 个字符' }]}
        >
          <Input placeholder="简要描述工作空间用途" maxLength={512} />
        </Form.Item>
        <Form.Item name="background" label="工作空间背景">
          <TextArea placeholder="工作空间的行业背景、技术栈、团队规模等信息" rows={4} />
        </Form.Item>
      </Form>
    </Modal>
  );
}
