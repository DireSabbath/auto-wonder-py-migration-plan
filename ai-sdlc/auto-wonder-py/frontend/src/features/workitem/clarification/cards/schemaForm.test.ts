import { describe, it, expect } from 'vitest';
import { parseElicitationSchema } from './schemaForm';

// 实测 qodercli 1.1.25 的真实载荷，不要替换成合成夹具：
// description 里逐行塞选项解释是 Qoder 的实际做法。
const qoderSchema = {
  type: 'object',
  title: 'Answer Questions',
  properties: {
    q0: {
      type: 'string',
      title: '技术栈',
      description:
        '项目采用什么技术栈?(仓库目前是空的,需要先定基础)\nNext.js 全栈 (推荐): 前后端一体,自带 API 路由。\nExpress + React 分离: 经典分离架构。\n其他: 请在补充说明中写明。',
      oneOf: [
        { const: 'Next.js 全栈 (推荐)', title: 'Next.js 全栈 (推荐)' },
        { const: 'Express + React 分离', title: 'Express + React 分离' },
        { const: '其他', title: '其他' },
      ],
    },
    q1: { type: 'string', title: '补充说明' },
  },
  required: ['q0'],
};

describe('parseElicitationSchema', () => {
  // F8
  it('maps oneOf string to a single-select field', () => {
    const form = parseElicitationSchema(qoderSchema);
    const q0 = form.fields.find((f) => f.name === 'q0')!;
    expect(q0.control).toBe('select');
    expect(q0.label).toBe('技术栈');
    expect(q0.required).toBe(true);
    expect(q0.options?.map((o) => o.value)).toEqual([
      'Next.js 全栈 (推荐)',
      'Express + React 分离',
      '其他',
    ]);
    expect(form.fallback).toBe(false);
    expect(form.title).toBe('Answer Questions');
  });

  // F12：选项解释必须拆出来挂到对应选项上，否则用户看到的是一大坨
  // 混在问题里的文本。
  it('splits option explanations out of description onto each option', () => {
    const form = parseElicitationSchema(qoderSchema);
    const q0 = form.fields.find((f) => f.name === 'q0')!;
    expect(q0.description).toBe('项目采用什么技术栈?(仓库目前是空的,需要先定基础)');
    const next = q0.options!.find((o) => o.value === 'Next.js 全栈 (推荐)')!;
    expect(next.hint).toBe('前后端一体,自带 API 路由。');
    const other = q0.options!.find((o) => o.value === '其他')!;
    expect(other.hint).toBe('请在补充说明中写明。');
  });

  it('keeps unmatched description lines in the question body', () => {
    const form = parseElicitationSchema({
      type: 'object',
      properties: {
        q0: {
          type: 'string',
          description: '选一个\n注意:这不是选项行\nA: 甲说明\nB:\n随手补一句',
          oneOf: [
            { const: 'a', title: 'A' },
            { const: 'b', title: 'B' },
          ],
        },
      },
    });
    const q0 = form.fields[0];
    expect(q0.description).toBe('选一个\n注意:这不是选项行\nB:\n随手补一句');
    expect(q0.options![0].hint).toBe('甲说明');
    expect(q0.options![1].hint).toBeUndefined();
  });

  it('accepts full-width colons and falls back to const when a title is absent', () => {
    const form = parseElicitationSchema({
      type: 'object',
      properties: {
        q0: {
          type: 'string',
          description: '选一个\n甲：全角冒号说明',
          oneOf: [{ const: '甲' }],
        },
      },
    });
    const q0 = form.fields[0];
    expect(q0.options![0].label).toBe('甲');
    expect(q0.options![0].hint).toBe('全角冒号说明');
    expect(q0.description).toBe('选一个');
  });

  // F9
  it('maps plain string to a text field', () => {
    const form = parseElicitationSchema(qoderSchema);
    const q1 = form.fields.find((f) => f.name === 'q1')!;
    expect(q1.control).toBe('text');
    expect(q1.required).toBe(false);
    expect(q1.description).toBeUndefined();
  });

  it('keeps the description as-is for fields without options', () => {
    const form = parseElicitationSchema({
      type: 'object',
      properties: { q0: { type: 'string', description: '第一行\n第二行' } },
    });
    expect(form.fields[0].description).toBe('第一行\n第二行');
    expect(form.fields[0].label).toBe('q0');
  });

  it('maps boolean to a switch and number to numeric input', () => {
    const form = parseElicitationSchema({
      type: 'object',
      properties: {
        a: { type: 'boolean', title: '开关' },
        b: { type: 'integer', title: '数量' },
        c: { type: 'number', title: '比例' },
      },
    });
    expect(form.fields.find((f) => f.name === 'a')!.control).toBe('boolean');
    expect(form.fields.find((f) => f.name === 'b')!.control).toBe('number');
    expect(form.fields.find((f) => f.name === 'c')!.control).toBe('number');
  });

  it('maps array of oneOf to multi-select', () => {
    const form = parseElicitationSchema({
      type: 'object',
      properties: {
        tags: {
          type: 'array',
          title: '标签',
          items: { oneOf: [{ const: 'x', title: 'X' }, { const: 'y', title: 'Y' }] },
        },
      },
    });
    const tags = form.fields.find((f) => f.name === 'tags')!;
    expect(tags.control).toBe('multiselect');
    expect(tags.options?.map((o) => o.label)).toEqual(['X', 'Y']);
  });

  // 实测 qodercli 1.1.31：多选题用 items.anyOf（不是 oneOf），并带 minItems/maxItems。
  // 旧解析只认 items.oneOf，真实多选载荷因此降级成 JSON 文本域——用户看到一个大输入框、没有 checkbox。
  it('maps array of anyOf (real qodercli multiselect shape) to multi-select', () => {
    const form = parseElicitationSchema({
      type: 'object',
      properties: {
        q0: {
          type: 'array',
          title: '交付物',
          description: '需要哪些交付物?\n代码: 实现代码。\n文档: 设计文档。\n测试: 单元测试。',
          minItems: 1,
          maxItems: 3,
          items: {
            anyOf: [
              { const: '代码', title: '代码' },
              { const: '文档', title: '文档' },
              { const: '测试', title: '测试' },
            ],
          },
        },
      },
      required: ['q0'],
    });
    const q0 = form.fields.find((f) => f.name === 'q0')!;
    expect(q0.control).toBe('multiselect');
    expect(q0.options?.map((o) => o.value)).toEqual(['代码', '文档', '测试']);
  });

  // 多选题的 description 同样逐行塞选项解释，必须和单选一样拆到各选项 hint 上。
  it('splits option explanations onto multiselect options like single-select', () => {
    const form = parseElicitationSchema({
      type: 'object',
      properties: {
        q0: {
          type: 'array',
          title: '交付物',
          description: '需要哪些交付物?\n代码: 实现代码。\n文档: 设计文档。',
          items: {
            anyOf: [
              { const: '代码', title: '代码' },
              { const: '文档', title: '文档' },
            ],
          },
        },
      },
    });
    const q0 = form.fields.find((f) => f.name === 'q0')!;
    expect(q0.control).toBe('multiselect');
    expect(q0.description).toBe('需要哪些交付物?');
    expect(q0.options!.find((o) => o.value === '代码')!.hint).toBe('实现代码。');
    expect(q0.options!.find((o) => o.value === '文档')!.hint).toBe('设计文档。');
  });

  // 单个字段认不出时只降级该字段，其余问题仍然可答。
  it('degrades only the unrecognised property to a json field', () => {
    const form = parseElicitationSchema({
      type: 'object',
      properties: {
        q0: { type: 'string' },
        weird: { type: 'array', items: { type: 'string' } },
        broken: 'not-a-schema',
      },
    });
    expect(form.fallback).toBe(false);
    expect(form.fields.map((f) => f.control)).toEqual(['text', 'json', 'json']);
    expect(form.fields[1].rawSchema).toEqual({ type: 'array', items: { type: 'string' } });
  });

  it('treats an empty oneOf as a plain text field', () => {
    const form = parseElicitationSchema({
      type: 'object',
      properties: { q0: { type: 'string', oneOf: [] } },
    });
    expect(form.fields[0].control).toBe('text');
  });

  it('skips oneOf entries without a const and degrades when none survive', () => {
    const form = parseElicitationSchema({
      type: 'object',
      properties: {
        q0: { type: 'string', oneOf: [{ title: '没有 const' }, { const: 'a', title: 'A' }] },
        q1: { type: 'string', oneOf: [{ const: null }, 'junk'] },
      },
    });
    expect(form.fields[0].control).toBe('select');
    expect(form.fields[0].options).toHaveLength(1);
    expect(form.fields[1].control).toBe('text');
  });

  it('leaves description undefined when a select carries no description', () => {
    const form = parseElicitationSchema({
      type: 'object',
      properties: { q0: { type: 'string', oneOf: [{ const: 'a', title: 'A' }] } },
    });
    expect(form.fields[0].control).toBe('select');
    expect(form.fields[0].description).toBeUndefined();
  });

  // F11：认不出的 schema 不能白屏，降级成 JSON 文本域让用户仍能作答。
  it('falls back to a raw JSON field for unrecognised schemas', () => {
    const form = parseElicitationSchema({ type: 'array', items: { type: 'string' } });
    expect(form.fallback).toBe(true);
    expect(form.fields).toHaveLength(1);
    expect(form.fields[0].control).toBe('json');
    expect(form.fields[0].rawSchema).toEqual({ type: 'array', items: { type: 'string' } });
  });

  it('falls back for null or malformed input', () => {
    expect(parseElicitationSchema(null).fallback).toBe(true);
    expect(parseElicitationSchema(undefined).fallback).toBe(true);
    expect(parseElicitationSchema('a string').fallback).toBe(true);
    expect(parseElicitationSchema({ type: 'object' }).fallback).toBe(true);
    expect(parseElicitationSchema({ type: 'object', properties: {} }).fallback).toBe(true);
  });

  it('ignores a non-array required list', () => {
    const form = parseElicitationSchema({
      type: 'object',
      properties: { q0: { type: 'string' } },
      required: 'q0',
    });
    expect(form.fields[0].required).toBe(false);
  });

  // 属性顺序必须稳定：q0/q1/q2 的提问顺序有语义。
  it('preserves property order', () => {
    const form = parseElicitationSchema({
      type: 'object',
      properties: { q2: { type: 'string' }, q0: { type: 'string' }, q1: { type: 'string' } },
    });
    expect(form.fields.map((f) => f.name)).toEqual(['q2', 'q0', 'q1']);
  });

  it('exposes the original schema for the folded raw view', () => {
    const form = parseElicitationSchema(qoderSchema);
    expect(form.raw).toBe(qoderSchema);
  });
});
